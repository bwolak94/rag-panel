"""node_upsert — upsert chunk vectors into Qdrant via RetrievalService.

IMPORTANT: This node MUST NOT import qdrant_client directly.
All Qdrant access goes through RetrievalService (enforced by CI architecture test).

Each point payload contains tenant_id for mandatory tenant-scoped filtering in retrieval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.db.models.collection import Collection
from src.db.models.models_registry import ModelsRegistry
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState
from src.retrieval.schemas import QdrantPoint, TenantContext
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)


async def node_upsert(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Upsert chunk embeddings into Qdrant via RetrievalService.

    Args:
        state: Must have chunks and embeddings from node_chunk and node_embed.
        config: RunnableConfig with configurable["db"] and configurable["retrieval"].

    Returns:
        {"point_ids": list[UUID]}

    Raises:
        IngestNodeError: On Qdrant upsert failure.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    retrieval: RetrievalService = cfg["retrieval"]
    step_start = utcnow()

    try:
        chunks = state.chunks or []
        embeddings = state.embeddings or []

        if not chunks:
            raise IngestNodeError("no chunks to upsert — node_chunk must run first")
        if len(chunks) != len(embeddings):
            raise IngestNodeError(
                f"chunk/embedding mismatch: {len(chunks)} chunks vs {len(embeddings)} vectors"
            )

        # Resolve Qdrant collection name: emb_{model_slug}
        model_name_q = (
            select(ModelsRegistry.name)
            .join(Collection, Collection.embedding_model_id == ModelsRegistry.id)
            .where(
                Collection.id == state.collection_id,
                Collection.tenant_id == state.tenant_id,
            )
        )
        model_name: str | None = (await session.execute(model_name_q)).scalar_one_or_none()
        if model_name is None:
            raise IngestNodeError(
                f"collection {state.collection_id} not found or has no embedding model"
            )
        model_slug = model_name.lower().replace("-", "_").replace(" ", "_")
        qdrant_collection = f"emb_{model_slug}"

        ctx = TenantContext(
            tenant_id=state.tenant_id,
            allowed_collection_ids=[state.collection_id],
        )
        points: list[QdrantPoint] = []
        now_ts = int(utcnow().timestamp())

        for chunk, vector in zip(chunks, embeddings, strict=True):
            points.append(
                QdrantPoint(
                    id=chunk.point_id,
                    vector=vector,
                    payload={
                        "tenant_id": str(state.tenant_id),
                        "collection_id": str(state.collection_id),
                        "document_id": str(state.document_id),
                        "chunk_index": chunk.chunk_index,
                        "page": chunk.page,
                        "section": chunk.section,
                        "text": chunk.text,
                        "created_at": now_ts,
                    },
                )
            )

        await retrieval.upsert_batch(ctx, qdrant_collection, points)

        point_ids: list[UUID] = [p.id for p in points]
        await update_step(
            session,
            state.job_id,
            stage="upsert",
            status="completed",
            started_at=step_start,
            meta={
                "points_upserted": len(points),
                "latency_ms": _elapsed_ms(step_start),
            },
        )
        logger.info(
            "node_upsert_completed",
            document_id=str(state.document_id),
            points_upserted=len(points),
        )
        return {"point_ids": point_ids}

    except IngestNodeError as exc:
        await update_step(
            session,
            state.job_id,
            stage="upsert",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"upsert_error: {type(exc).__name__}"
        await update_step(
            session,
            state.job_id,
            stage="upsert",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)
