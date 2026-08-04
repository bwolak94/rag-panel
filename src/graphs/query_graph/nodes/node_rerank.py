"""node_rerank — LLM-as-reranker: re-score retrieved chunks against the question.

This node sits between node_retrieve and node_grade_documents.  It sends all
retrieved chunks to the LLM in a single batch call and asks for relevance
scores (0-10).  Chunks are then sorted by score descending so the highest-
quality context reaches node_grade_documents first.

The node is opt-in: when ``state.rerank_enabled`` is False (default) the node
is a transparent pass-through and the LLM is never called.

Fail-safe: any error during scoring (LLM unavailable, parse failure, partial
scores) is logged as a warning and the original chunk order is preserved.
The pipeline never hard-fails due to reranking.

GDPR:
- Never log question text, chunk text, or rerank scores mapped to content.
- Log only tenant_id, chunk counts, and latency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "rerank_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _build_rerank_prompt(question: str, chunks: list[dict[str, Any]]) -> str:
    """Render the rerank prompt with the question and numbered chunk list.

    Each chunk is wrapped in an XML element so the LLM can unambiguously
    reference them by index.  Chunk content is treated as untrusted data
    per the security rules in the prompt template.

    Args:
        question: The (possibly rewritten) question string.
        chunks: List of chunk dicts, each with a ``highlight_text`` key.

    Returns:
        Fully rendered prompt string ready to be sent as a system message.
    """
    base_prompt = _load_prompt()

    chunks_text = "\n\n".join(
        f'<chunk index="{i + 1}">{chunk.get("highlight_text", "")}</chunk>'
        for i, chunk in enumerate(chunks)
    )

    return base_prompt.replace("{{QUESTION}}", question).replace("{{CHUNKS}}", chunks_text)


def _apply_scores(
    chunks: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    top_k: int | None,
) -> list[dict[str, Any]]:
    """Merge LLM scores into chunks and sort by score descending.

    Chunks whose index was not returned by the LLM retain a ``rerank_score``
    of 0 so they sort to the bottom (graceful partial-score handling).

    Args:
        chunks: Original chunk list (0-indexed in list, 1-indexed in prompt).
        scores: Parsed ``scores`` array from the LLM response.
        top_k: If set, truncate the sorted list to this length.

    Returns:
        New list of chunk dicts with an added ``rerank_score`` field, ordered
        by score descending and optionally truncated to top_k.
    """
    # Build index → score map from the LLM response (1-based chunk_index).
    score_map: dict[int, int] = {}
    for entry in scores:
        idx = entry.get("chunk_index")
        raw_score = entry.get("score")
        if isinstance(idx, int) and isinstance(raw_score, int | float):
            # Clamp to [0, 10] to guard against out-of-range values.
            score_map[idx] = max(0, min(10, int(raw_score)))

    scored: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks, start=1):
        chunk_with_score = dict(chunk)
        chunk_with_score["rerank_score"] = score_map.get(i, 0)
        scored.append(chunk_with_score)

    scored.sort(key=lambda c: c["rerank_score"], reverse=True)

    if top_k is not None and top_k > 0:
        scored = scored[:top_k]

    return scored


@observe(capture_input=False, capture_output=False)
async def node_rerank(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Re-score and re-order retrieved chunks using an LLM relevance scorer.

    When ``state.rerank_enabled`` is False this node returns the chunks
    unchanged (pass-through) without any LLM call.

    Args:
        state: Must have ``retrieved_chunks``, ``question``, ``llm_model_id``,
               ``rerank_enabled``, and optionally ``rerank_top_k``.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        ``{"retrieved_chunks": list[dict]}`` — the (possibly reordered) chunks.
        Each chunk dict gains a ``rerank_score`` field when reranking is active.
    """
    if not state.rerank_enabled:
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "rerank_enabled": False,
                "chunk_count_in": len(state.retrieved_chunks),
                "chunk_count_out": len(state.retrieved_chunks),
            }
        )
        logger.info(
            "node_rerank.skipped",
            tenant_id=str(state.tenant_id),
            chunk_count=len(state.retrieved_chunks),
        )
        return {"retrieved_chunks": state.retrieved_chunks}

    chunks = state.retrieved_chunks
    if not chunks:
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "rerank_enabled": True,
                "chunk_count_in": 0,
                "chunk_count_out": 0,
            }
        )
        logger.info(
            "node_rerank.no_chunks",
            tenant_id=str(state.tenant_id),
        )
        return {"retrieved_chunks": []}

    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    node_start = datetime.now(UTC)

    # Resolve the LLM model record from the registry.
    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        # Fail-safe: model missing → return original order.
        logger.warning(
            "node_rerank.model_not_found_fallback",
            tenant_id=str(state.tenant_id),
            llm_model_id=str(state.llm_model_id),
        )
        return {"retrieved_chunks": chunks}

    question = state.rewritten_query or state.question
    prompt = _build_rerank_prompt(question, chunks)

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
        scores: list[dict[str, Any]] = parsed.get("scores", [])
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        logger.warning(
            "node_rerank.parse_error_fallback",
            tenant_id=str(state.tenant_id),
            chunk_count=len(chunks),
            error=type(exc).__name__,
        )
        return {"retrieved_chunks": chunks}
    except Exception as exc:
        logger.warning(
            "node_rerank.llm_error_fallback",
            tenant_id=str(state.tenant_id),
            chunk_count=len(chunks),
            error=type(exc).__name__,
        )
        return {"retrieved_chunks": chunks}

    reranked = _apply_scores(chunks, scores, state.rerank_top_k)

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "model_id": str(model_record.id),
            "rerank_enabled": True,
            "chunk_count_in": len(chunks),
            "chunk_count_out": len(reranked),
            "rerank_top_k": state.rerank_top_k,
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_rerank.completed",
        tenant_id=str(state.tenant_id),
        chunk_count_in=len(chunks),
        chunk_count_out=len(reranked),
        latency_ms=elapsed_ms,
    )

    return {"retrieved_chunks": reranked}
