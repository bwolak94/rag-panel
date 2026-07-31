"""node_research_synthesize — synthesise a comprehensive answer from all accumulated chunks.

Receives all chunks gathered across every research iteration and produces a final answer
with citations. This is the terminal node: it sets final_answer, citations, prompt_tokens,
and completion_tokens on the state.

GDPR:
- Never log question text, answer text, chunk content, or citation text.
- Log only tenant_id, pipeline_id, chunk count, token counts, citation count, latency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.research_graph.state import ResearchState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "research_synthesize_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _build_context_chunks(chunks: list[dict[str, Any]]) -> str:
    """Format all accumulated chunks with XML delimiters for the synthesis prompt.

    Uses a global index across all iterations so the LLM can cite by sequential number.
    """
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        doc_id = chunk.get("document_id", "unknown")
        page = chunk.get("page_number", "")
        text = chunk.get("highlight_text", "")
        parts.append(f'<chunk index="{i}" document_id="{doc_id}" page="{page}">{text}</chunk>')
    return "\n\n".join(parts)


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "(brak historii)"
    return "\n".join(f"{entry['role']}: {entry['content']}" for entry in history)


def _map_citation_to_source(
    citation: dict[str, Any], chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map an LLM citation (chunk_index) to a MessageSourceOut-compatible dict.

    Mirrors the mapping in node_generate for API-level consistency.
    """
    chunk_index = citation.get("chunk_index")
    if not isinstance(chunk_index, int) or chunk_index < 1 or chunk_index > len(chunks):
        return {}

    chunk = chunks[chunk_index - 1]
    payload = chunk.get("payload", {})

    return {
        "document_id": chunk.get("document_id", ""),
        "collection_id": chunk.get("collection_id") or payload.get("collection_id", ""),
        "document_title": payload.get("document_title") or payload.get("filename", ""),
        "section_heading": payload.get("section") or payload.get("heading"),
        "source_url": payload.get("source_url"),
        "chunk_id": str(chunk.get("chunk_id")) if chunk.get("chunk_id") else None,
        "page_number": chunk.get("page_number"),
        "highlight_text": chunk.get("highlight_text"),
        "relevance_score": chunk.get("score", 0.0),
    }


@observe(as_type="generation", capture_input=False, capture_output=False)
async def node_research_synthesize(state: ResearchState, config: dict[str, Any]) -> dict[str, Any]:
    """Synthesise a comprehensive answer from all chunks accumulated across iterations.

    Builds context from state.all_retrieved_chunks, calls the LLM with the synthesis
    prompt, and maps citations back to source metadata dicts.

    Args:
        state: Must have original_question, all_retrieved_chunks, llm_model_id,
               conversation_history, steps_taken.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        dict with keys: final_answer, citations, prompt_tokens, completion_tokens.

    Raises:
        QueryNodeError: On LLM failure, JSON parse error, or missing model record.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    llm = cfg["llm"]

    node_start = datetime.now(UTC)

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(
            f"research_synthesize: LLM model not found: llm_model_id={state.llm_model_id}"
        )

    chunks = state.all_retrieved_chunks
    context_text = _build_context_chunks(chunks) if chunks else "(brak kontekstu)"
    history_text = _format_history(state.conversation_history)

    prompt = (
        _load_prompt()
        .replace("{{ORIGINAL_QUESTION}}", state.original_question)
        .replace("{{CONTEXT_CHUNKS}}", context_text)
        .replace("{{CONVERSATION_HISTORY}}", history_text)
        .replace("{{STEPS_TAKEN}}", str(state.steps_taken))
    )

    try:
        response = await llm.chat_completion(
            model=model_record.model_id,
            messages=[{"role": "system", "content": prompt}],
            base_url=model_record.endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
        final_answer: str = parsed.get("answer", "")
        llm_citations: list[dict[str, Any]] = parsed.get("citations", [])
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        raise QueryNodeError(f"research_synthesize_parse_error: {type(exc).__name__}") from exc
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"research_synthesize_llm_error: {type(exc).__name__}") from exc

    prompt_tokens = 0
    completion_tokens = 0
    if hasattr(response, "usage") and response.usage is not None:
        prompt_tokens = response.usage.prompt_tokens or 0
        completion_tokens = response.usage.completion_tokens or 0

    citations: list[dict[str, Any]] = []
    for cit in llm_citations:
        mapped = _map_citation_to_source(cit, chunks)
        if mapped:
            citations.append(mapped)

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "pipeline_id": str(state.pipeline_id),
            "model_id": str(model_record.id),
            "total_chunks_in": len(chunks),
            "citation_count": len(citations),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "steps_taken": state.steps_taken,
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_research_synthesize.completed",
        tenant_id=str(state.tenant_id),
        pipeline_id=str(state.pipeline_id),
        total_chunks_in=len(chunks),
        citation_count=len(citations),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        steps_taken=state.steps_taken,
        latency_ms=elapsed_ms,
    )

    return {
        "final_answer": final_answer,
        "citations": citations,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
