"""node_generate — generate the final answer using graded context chunks.

Calls the LLM with graded chunks as context. Parses JSON response with
"answer" and "citations" array. Maps citations to MessageSourceOut format.

GDPR:
- Never log question, answer, or chunk text content.
- Log only tenant_id, token counts, chunk count, latency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "generate_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "(brak historii)"
    return "\n".join(f"{entry['role']}: {entry['content']}" for entry in history)


def _build_context_chunks(chunks: list[dict[str, Any]]) -> str:
    """Format graded chunks with XML delimiters for the prompt."""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        doc_id = chunk.get("document_id", "unknown")
        page = chunk.get("page_number", "")
        text = chunk.get("highlight_text", "")
        parts.append(f'<chunk index="{i}" document_id="{doc_id}" page="{page}">{text}</chunk>')
    return "\n\n".join(parts)


def _map_citation_to_source(
    citation: dict[str, Any], chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map an LLM citation (chunk_index) to a MessageSourceOut-compatible dict.

    Extracts document_id, collection_id, page_number, highlight_text
    from the chunk payload at the cited index.
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


async def node_generate(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Generate the final RAG answer with citations.

    Args:
        state: Must have graded_chunks, question, llm_model_id, conversation_history.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        {"answer": str, "citations": list[dict], "prompt_tokens": int, "completion_tokens": int}

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    node_start = datetime.now(UTC)

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(
        select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id)
    )
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    context_chunks_text = _build_context_chunks(state.graded_chunks)
    history_text = _format_history(state.conversation_history)

    prompt = (
        _load_prompt()
        .replace("{{QUESTION}}", state.question)
        .replace("{{CONTEXT_CHUNKS}}", context_chunks_text)
        .replace("{{CONVERSATION_HISTORY}}", history_text)
    )

    try:
        response = await llm.chat_completion(
            model=model_record.model_id,
            messages=[
                {"role": "system", "content": prompt},
            ],
            base_url=model_record.endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
        answer: str = parsed.get("answer", "")
        llm_citations: list[dict[str, Any]] = parsed.get("citations", [])
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        raise QueryNodeError(f"generate_parse_error: {type(exc).__name__}") from exc
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"generate_llm_error: {type(exc).__name__}") from exc

    # Extract token counts from response
    prompt_tokens = 0
    completion_tokens = 0
    if hasattr(response, "usage") and response.usage is not None:
        prompt_tokens = response.usage.prompt_tokens or 0
        completion_tokens = response.usage.completion_tokens or 0

    # Map LLM citations to source dicts
    citations: list[dict[str, Any]] = []
    for cit in llm_citations:
        mapped = _map_citation_to_source(cit, state.graded_chunks)
        if mapped:
            citations.append(mapped)

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    logger.info(
        "node_generate.completed",
        tenant_id=str(state.tenant_id),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        citation_count=len(citations),
        latency_ms=elapsed_ms,
    )

    return {
        "answer": answer,
        "citations": citations,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }
