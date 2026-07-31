"""node_retrieve — embed the rewritten query and retrieve chunks from Qdrant.

This node is the only place in the query graph that calls RetrievalService.
Tenant isolation is enforced via TenantContext(allowed_collection_ids).

GDPR:
- Never log rewritten_query or chunk text.
- Log only tenant_id, chunk count, collection name, latency.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState
from src.retrieval.exceptions import EmptyCollectionListError
from src.retrieval.schemas import SearchMode, TenantContext

logger = structlog.get_logger(__name__)

_DEFAULT_TOP_K = 8
_DEFAULT_SCORE_THRESHOLD = 0.35


def _make_qdrant_collection_name(model_record: Any) -> str:
    """Derive Qdrant collection name from model record.

    Uses model_record.params["slug"] if available, otherwise derives from model_id
    by lowercasing and replacing / and - with _.
    """
    slug = model_record.params.get("slug") if model_record.params else None
    if slug:
        return f"emb_{slug}"
    derived = model_record.model_id.replace("/", "_").replace("-", "_").lower()
    return f"emb_{derived}"


def _resolve_search_mode(search_config: dict[str, Any] | None) -> SearchMode:
    """Extract SearchMode from a collection's search_config JSON field.

    Returns SearchMode.DENSE if the field is absent or contains an unknown value.
    """
    if not search_config:
        return SearchMode.DENSE
    raw = search_config.get("search_mode", "dense")
    try:
        return SearchMode(raw)
    except ValueError:
        logger.warning("node_retrieve.unknown_search_mode", raw_value=raw)
        return SearchMode.DENSE


@observe(capture_input=False, capture_output=False)
async def node_retrieve(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Embed the rewritten query and search Qdrant for relevant chunks.

    Args:
        state: Must have rewritten_query, collection_ids, tenant_id, allowed_collection_ids.
        config: RunnableConfig with configurable["db"], configurable["llm"],
                configurable["retrieval"].

    Returns:
        {"retrieved_chunks": list[dict], "embedding_model_id": UUID, "qdrant_collection": str}

    Raises:
        QueryNodeError: On embedding failure or Qdrant unreachable.
        EmptyCollectionListError: Re-raised if allowed_collection_ids is empty.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    llm = cfg["llm"]
    retrieval = cfg["retrieval"]

    node_start = datetime.now(UTC)

    # Resolve embedding model from first collection in pipeline
    if not state.collection_ids:
        raise QueryNodeError("pipeline has no collection_ids configured")

    from sqlalchemy import select

    from src.db.models.collection import Collection
    from src.db.models.models_registry import ModelsRegistry

    collection_result = await db.execute(
        select(Collection).where(Collection.id == state.collection_ids[0])
    )
    collection = collection_result.scalar_one_or_none()
    if collection is None:
        raise QueryNodeError(f"collection not found: {state.collection_ids[0]}")

    model_result = await db.execute(
        select(ModelsRegistry).where(ModelsRegistry.id == collection.embedding_model_id)
    )
    model_record = model_result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"embedding model not found: {collection.embedding_model_id}")

    qdrant_collection = _make_qdrant_collection_name(model_record)
    query_text = state.rewritten_query or state.question

    # Embed the query
    try:
        embed_response = await llm.embeddings(
            model=model_record.model_id,
            input=[query_text],
            base_url=model_record.endpoint_url,
        )
        query_vector: list[float] = embed_response.data[0].embedding
    except Exception as exc:
        raise QueryNodeError(f"embedding_error: {type(exc).__name__}") from exc

    # Resolve search configuration from the collection's search_config JSON column.
    search_config: dict[str, Any] | None = getattr(collection, "search_config", None)
    search_mode = _resolve_search_mode(search_config)
    top_k: int = int((search_config or {}).get("top_k", _DEFAULT_TOP_K))
    score_threshold: float = float(
        (search_config or {}).get("score_threshold", _DEFAULT_SCORE_THRESHOLD)
    )

    # Build tenant context
    tenant_ctx = TenantContext(
        tenant_id=state.tenant_id,
        allowed_collection_ids=list(state.allowed_collection_ids),
    )

    # Search Qdrant — dispatch to hybrid or dense based on collection config.
    try:
        results = await retrieval.search(
            ctx=tenant_ctx,
            qdrant_collection=qdrant_collection,
            query_vector=query_vector,
            top_k=top_k,
            score_threshold=score_threshold,
            search_mode=search_mode,
            query_text=query_text,
        )
    except EmptyCollectionListError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"retrieval_error: {type(exc).__name__}") from exc

    # Serialize results into dicts for state (UUID -> str for JSON compatibility)
    retrieved_chunks: list[dict[str, Any]] = []
    for r in results:
        chunk: dict[str, Any] = {
            "point_id": str(r.point_id),
            "document_id": str(r.document_id),
            "score": r.score,
            "page_number": r.page_number,
            "highlight_text": r.highlight_text,
            "collection_id": str(r.collection_id) if r.collection_id else None,
            "payload": r.payload,
        }
        retrieved_chunks.append(chunk)

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "embedding_model_id": str(model_record.id),
            "qdrant_collection": qdrant_collection,
            "search_mode": search_mode.value,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "chunk_count": len(retrieved_chunks),
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_retrieve.completed",
        tenant_id=str(state.tenant_id),
        qdrant_collection=qdrant_collection,
        chunk_count=len(retrieved_chunks),
        search_mode=search_mode.value,
        latency_ms=elapsed_ms,
    )

    return {
        "retrieved_chunks": retrieved_chunks,
        "embedding_model_id": UUID(str(model_record.id)),
        "qdrant_collection": qdrant_collection,
    }
