"""node_generate — generate the final answer using graded context chunks.

Calls the LLM with graded chunks as context. Parses JSON response with
"answer" and "citations" array. Maps citations to MessageSourceOut format.

Before building the context string the node applies a token budget: chunks
are sorted by score (descending) and the lowest-scoring chunks are dropped
until the total context token count fits within the budget.  The budget is
resolved from (in priority order):
  1. state.context_token_budget  (set by the caller / pipeline config)
  2. pipeline.guardrails_config["context_token_budget"]  (per-pipeline override)
  3. Settings.DEFAULT_CONTEXT_TOKEN_BUDGET  (global fallback, default 6 000)
When state.context_token_budget is explicitly None and no pipeline override
exists, the global fallback is used.

AB testing (shadow mode)
------------------------
When ``state.prompt_config`` contains a valid ``ab_test`` block with
``enabled=true`` and the traffic-split coin-flip succeeds,
``domain.ab_testing_service.should_run_shadow()`` returns ``True`` and
``domain.ab_testing_service.schedule_shadow_task()`` launches a background
``asyncio.Task`` that runs the shadow prompt after the main answer has already
been produced.  The user response is never delayed by this.

GDPR:
- Never log question, answer, or chunk text content.
- Log only tenant_id, token counts, chunk count, latency.
- AB-test shadow task stores only answer lengths and latencies — never text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import tiktoken
from langfuse import observe

from src.core.config import settings
from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.domain.ab_testing_service import schedule_shadow_task, should_run_shadow
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_prompt_cache: dict[str, str] = {}


def _load_prompt(version: str = "v1") -> str:
    """Load and cache a versioned generate prompt from disk.

    Args:
        version: Prompt file version suffix, e.g. ``"v1"`` or ``"v2"``.
            The file ``src/graphs/prompts/generate_<version>.md`` must exist.

    Returns:
        The prompt file contents as a string.
    """
    if version not in _prompt_cache:
        path = _PROMPTS_DIR / f"generate_{version}.md"
        _prompt_cache[version] = path.read_text(encoding="utf-8")
    return _prompt_cache[version]


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Return the number of tokens in *text* using the specified tiktoken encoding.

    Args:
        text: Plain text to tokenise.
        encoding_name: tiktoken encoding name; defaults to cl100k_base which
            covers GPT-3.5/4 and is a reasonable approximation for other models.

    Returns:
        Integer token count.
    """
    enc = tiktoken.get_encoding(encoding_name)
    return len(enc.encode(text))


def _trim_chunks_to_budget(
    chunks: list[dict[str, Any]], budget: int
) -> tuple[list[dict[str, Any]], int]:
    """Keep the highest-scoring chunks whose combined context tokens fit *budget*.

    Chunks are sorted by ``score`` descending so that the most relevant context
    is always retained.  Lower-scoring chunks are dropped when the running token
    total would exceed the budget.

    Args:
        chunks: Graded chunks list (each dict must have ``highlight_text`` and
            optionally ``score``).
        budget: Maximum total token count for the assembled context.

    Returns:
        A tuple of ``(kept_chunks, dropped_count)`` where *kept_chunks*
        preserves the original ordering (score-sorted order is used only for
        the selection decision).
    """
    if not chunks:
        return chunks, 0

    # Sort a copy by score descending; keep original index for stable re-sort
    scored = sorted(enumerate(chunks), key=lambda t: t[1].get("score", 0.0), reverse=True)

    kept_indices: set[int] = set()
    running_tokens = 0

    for original_idx, chunk in scored:
        text = chunk.get("highlight_text", "")
        token_count = _count_tokens(text)
        if running_tokens + token_count <= budget:
            kept_indices.add(original_idx)
            running_tokens += token_count

    # Rebuild list in original order
    kept = [c for i, c in enumerate(chunks) if i in kept_indices]
    dropped = len(chunks) - len(kept)
    return kept, dropped


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


def _resolve_token_budget(state: QueryState) -> int:
    """Return the effective context token budget for this request.

    Resolution order (first non-None value wins):
    1. ``state.context_token_budget`` — explicit per-request override.
    2. ``state.guardrails_config["context_token_budget"]`` — per-pipeline value
       stored in RagPipeline.guardrails_config and propagated into state by
       ChatService at graph invocation time.
    3. ``settings.DEFAULT_CONTEXT_TOKEN_BUDGET`` — global application default.
    """
    if state.context_token_budget is not None:
        return state.context_token_budget

    pipeline_budget = state.guardrails_config.get("context_token_budget")
    if pipeline_budget is not None:
        return int(pipeline_budget)

    return settings.DEFAULT_CONTEXT_TOKEN_BUDGET


@observe(as_type="generation", capture_input=False, capture_output=False)
async def node_generate(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Generate the final RAG answer with citations.

    Args:
        state: Must have graded_chunks, question, llm_model_id, conversation_history.
            Optional: context_token_budget, guardrails_config.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        dict with keys: answer, citations, prompt_tokens, completion_tokens,
        chunks_trimmed.

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]
    # session_factory is optional; only required when AB testing is active.
    session_factory = cfg.get("session_factory")

    node_start = datetime.now(UTC)

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    # --- Token budget: trim low-score chunks before building context ---
    budget = _resolve_token_budget(state)
    graded_chunks, chunks_dropped = _trim_chunks_to_budget(state.graded_chunks, budget)

    if chunks_dropped > 0:
        logger.warning(
            "node_generate.context_trimmed",
            tenant_id=str(state.tenant_id),
            pipeline_id=str(state.pipeline_id),
            chunks_before=len(state.graded_chunks),
            chunks_dropped=chunks_dropped,
            token_budget=budget,
        )

    context_chunks_text = _build_context_chunks(graded_chunks)
    history_text = _format_history(state.conversation_history)

    # Resolve prompt version: prompt_config.generate_prompt_version → "v1" default.
    control_version: str = state.prompt_config.get("generate_prompt_version", "v1")

    prompt = (
        _load_prompt(control_version)
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

    # Map LLM citations to source dicts (use trimmed chunk list so indices align)
    citations: list[dict[str, Any]] = []
    for cit in llm_citations:
        mapped = _map_citation_to_source(cit, graded_chunks)
        if mapped:
            citations.append(mapped)

    elapsed_ms = (datetime.now(UTC) - node_start).total_seconds() * 1000.0
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "model_id": str(model_record.id),
            "num_chunks_in": len(state.graded_chunks),
            "num_chunks_used": len(graded_chunks),
            "chunks_trimmed": chunks_dropped,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "citation_count": len(citations),
            "token_budget": budget,
            "latency_ms": int(elapsed_ms),
        }
    )
    logger.info(
        "node_generate.completed",
        tenant_id=str(state.tenant_id),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        citation_count=len(citations),
        chunks_trimmed=chunks_dropped,
        latency_ms=int(elapsed_ms),
    )

    # ------------------------------------------------------------------
    # AB testing — fire-and-forget shadow call (never delays response)
    # ------------------------------------------------------------------
    ab_config: dict[str, Any] = state.prompt_config.get("ab_test", {})
    if ab_config and session_factory is not None and should_run_shadow(state.prompt_config):
        schedule_shadow_task(
            state=state,
            ab_config=ab_config,
            control_version=control_version,
            control_answer_length=len(answer),
            latency_control_ms=elapsed_ms,
            llm_client=llm,
            model_record=model_record,
            session_factory=session_factory,
        )

    return {
        "answer": answer,
        "citations": citations,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "chunks_trimmed": chunks_dropped,
    }
