"""RetrievalService — the ONLY module with Qdrant access.

This stub exposes the interface used by CollectionService (TASK-005).
Full Qdrant implementation is in TASK-009.
"""

from __future__ import annotations

import structlog

from src.retrieval.schemas import QdrantPoint, TenantContext

logger = structlog.get_logger(__name__)


class RetrievalService:
    """Owns all Qdrant interactions.

    Rules (enforced by architecture):
    - No other module may import AsyncQdrantClient directly.
    - Every query must include both tenant_id and collection_id filters.
    - ensure_collection() is idempotent and creates payload indexes on first call.
    """

    async def ensure_collection(self, model_slug: str, vector_size: int) -> None:
        """Ensure a Qdrant collection named `emb_{model_slug}` exists.

        Creates the collection and required payload indexes (tenant_id, collection_id)
        if they do not already exist.  Idempotent — safe to call on every create.

        Full implementation: TASK-009.
        """
        logger.info(
            "ensure_collection_stub",
            qdrant_collection=f"emb_{model_slug}",
            vector_size=vector_size,
        )

    async def upsert_batch(self, ctx: TenantContext, points: list[QdrantPoint]) -> None:
        """Upsert a batch of Qdrant points into the tenant-scoped collection.

        The target collection is resolved from the embedding model slug stored
        in the collection record. Naming: ``emb_{embedding_model_slug}``.

        Payload MUST contain ``tenant_id`` field to allow mandatory tenant filter.

        Full implementation: TASK-009.
        """
        logger.info(
            "upsert_batch_stub",
            tenant_id=str(ctx.tenant_id),
            point_count=len(points),
        )
