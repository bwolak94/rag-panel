"""node_research_retrieve — embed the current sub-query and fetch chunks from Qdrant.

Wraps RetrievalService.search() with deduplication: any chunk whose point_id already
exists in state.all_retrieved_chunks is dropped before updating state. This prevents
the same evidence from being surfaced in the synthesis step multiple times.

All retrieval goes through RetrievalService — never direct Qdrant access (ADR-1).

GDPR:
- Never log sub-query text or chunk content.
- Log only tenant_id, pipeline_id, step, new/total chunk counts, latency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.research_graph.state import ResearchIteration, ResearchState
from src.retrieval.exceptions import EmptyCollectionListError
from src.retrieval.schemas import SearchMode, TenantContext

logger = structlog.get_logger(__name__)

_DEFAULT_TOP_K = 8
_DEFAULT_SCORE_THRESHOLD = 0.35


def _make_qdrant_collection_name(model_record: Any) -> str:
    """Derive Qdrant collection name from model record.

    Mirrors the logic in node_retrieve to ensure consistent naming.
    Uses model_record.params["slug"] when available; falls back to lowercased model_id.
    """
    slug = model_record.params.get("slug") if model_record.params else None
    if slug:
        return f"emb_{slug}"
    derived = model_record.model_id.replace("/", "_").replace("-", "_").lower()
    return f"emb_{derived}"


def _resolve_search_mode(search_config: dict[str, Any] | None) -> SearchMode:
    """Extract SearchMode from a collection's search_config JSON field."""
    if not search_config:
        return SearchMode.DENSE
    raw = search_config.get("search_mode", "dense")
    try:
        return SearchMode(raw)
    except ValueError:
        logger.warning("node_research_retrieve.unknown_search_mode", raw_value=raw)
        return SearchMode.DENSE


def _deduplicate_chunks(
    new_chunks: list[dict[str, Any]],
    existing_point_ids: set[str],
) -> list[dict[str, Any]]:
    """Drop chunks whose point_id is already present in the accumulated pool.

    Args:
        new_chunks: Freshly retrieved chunks from this iteration.
        existing_point_ids: Set of point_id strings already in all_retrieved_chunks.

    Returns:
        Subset of new_chunks that have not been seen before.
    """
    return [c for c in new_chunks if c.get("point_id") not in existing_point_ids]


@observe(capture_input=False, capture_output=False)
async def node_research_retrieve(state: ResearchState, config: dict[str, Any]) -> dict[str, Any]:
    """Embed the latest sub-query and search Qdrant; deduplicate against accumulated chunks.

    Takes the sub-query from the most-recent ResearchIteration (set by node_research_plan),
    embeds it, searches Qdrant, deduplicates against state.all_retrieved_chunks, and
    updates both all_retrieved_chunks and the current iteration's retrieved_chunks.

    Args:
        state: Must have iterations (at least one), collection_ids, tenant_id,
               allowed_collection_ids.
        config: RunnableConfig with configurable["db"], configurable["llm"],
                configurable["retrieval"].

    Returns:
        dict with keys: iterations (updated list with chunks on latest), all_retrieved_chunks.

    Raises:
        QueryNodeError: If no iterations exist, collection/model not found, or embed fails.
        EmptyCollectionListError: Re-raised if allowed_collection_ids is empty.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    llm = cfg["llm"]
    retrieval = cfg["retrieval"]

    node_start = datetime.now(UTC)

    if not state.iterations:
        raise QueryNodeError(
            "research_retrieve: no iterations in state — node_research_plan must run first"
        )

    current_iteration: ResearchIteration = state.iterations[-1]
    sub_query = current_iteration.query

    if not state.collection_ids:
        raise QueryNodeError("research_retrieve: pipeline has no collection_ids configured")

    from sqlalchemy import or_, select

    from src.db.models.collection import Collection
    from src.db.models.models_registry import ModelsRegistry

    collection_result = await db.execute(
        select(Collection).where(
            Collection.id == state.collection_ids[0],
            or_(
                Collection.tenant_id == state.tenant_id,
                Collection.is_public.is_(True),
            ),
        )
    )
    collection = collection_result.scalar_one_or_none()
    if collection is None:
        raise QueryNodeError(f"research_retrieve: collection not found: {state.collection_ids[0]}")

    model_result = await db.execute(
        select(ModelsRegistry).where(ModelsRegistry.id == collection.embedding_model_id)
    )
    model_record = model_result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(
            f"research_retrieve: embedding model not found: {collection.embedding_model_id}"
        )

    qdrant_collection = _make_qdrant_collection_name(model_record)

    try:
        embed_response = await llm.embeddings(
            model=model_record.model_id,
            input=[sub_query],
            base_url=model_record.endpoint_url,
        )
        query_vector: list[float] = embed_response.data[0].embedding
    except Exception as exc:
        raise QueryNodeError(f"research_retrieve_embedding_error: {type(exc).__name__}") from exc

    search_config: dict[str, Any] | None = getattr(collection, "search_config", None)
    search_mode = _resolve_search_mode(search_config)
    top_k: int = int((search_config or {}).get("top_k", _DEFAULT_TOP_K))
    score_threshold: float = float(
        (search_config or {}).get("score_threshold", _DEFAULT_SCORE_THRESHOLD)
    )

    tenant_ctx = TenantContext(
        tenant_id=state.tenant_id,
        allowed_collection_ids=list(state.allowed_collection_ids),
    )

    try:
        results = await retrieval.search(
            ctx=tenant_ctx,
            qdrant_collection=qdrant_collection,
            query_vector=query_vector,
            top_k=top_k,
            score_threshold=score_threshold,
            search_mode=search_mode,
            query_text=sub_query,
        )
    except EmptyCollectionListError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"research_retrieve_qdrant_error: {type(exc).__name__}") from exc

    # Serialise results to dicts (mirrors node_retrieve serialisation for consistency)
    raw_chunks: list[dict[str, Any]] = []
    for r in results:
        raw_chunks.append(
            {
                "point_id": str(r.point_id),
                "document_id": str(r.document_id),
                "score": r.score,
                "page_number": r.page_number,
                "highlight_text": r.highlight_text,
                "collection_id": str(r.collection_id) if r.collection_id else None,
                "payload": r.payload,
            }
        )

    # Deduplicate against all previously retrieved chunks
    existing_point_ids: set[str] = {str(c.get("point_id", "")) for c in state.all_retrieved_chunks}
    new_chunks = _deduplicate_chunks(raw_chunks, existing_point_ids)

    # Update the current iteration with retrieved chunks (copy to avoid mutation)
    updated_iteration = current_iteration.model_copy(update={"retrieved_chunks": new_chunks})
    updated_iterations = list(state.iterations[:-1]) + [updated_iteration]

    updated_all_chunks = list(state.all_retrieved_chunks) + new_chunks

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "pipeline_id": str(state.pipeline_id),
            "step": current_iteration.step,
            "raw_chunk_count": len(raw_chunks),
            "new_chunk_count": len(new_chunks),
            "total_chunk_count": len(updated_all_chunks),
            "qdrant_collection": qdrant_collection,
            "search_mode": search_mode.value,
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_research_retrieve.completed",
        tenant_id=str(state.tenant_id),
        pipeline_id=str(state.pipeline_id),
        step=current_iteration.step,
        raw_chunk_count=len(raw_chunks),
        new_chunk_count=len(new_chunks),
        total_chunk_count=len(updated_all_chunks),
        latency_ms=elapsed_ms,
    )

    return {
        "iterations": updated_iterations,
        "all_retrieved_chunks": updated_all_chunks,
    }
