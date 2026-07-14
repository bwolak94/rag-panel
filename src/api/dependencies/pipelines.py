"""Pipeline resolution dependency for the chat endpoint.

resolve_pipeline_by_model() looks up rag_pipelines by name within the user's tenant
and verifies collection-level access for the requesting user.
"""

from __future__ import annotations

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)


async def resolve_pipeline_by_model(
    model: str,
    ctx: UserContext,
    db: AsyncSession,
) -> RagPipeline:
    """Resolve a rag_pipeline by model name within the user's tenant.

    Verifies that all collection_ids in the pipeline are in user_ctx.allowed_collection_ids.

    Raises:
        HTTPException(404): pipeline not found or inactive.
        HTTPException(403): user lacks access to one of the pipeline's collections.
    """
    pipeline = await db.scalar(
        select(RagPipeline).where(
            RagPipeline.tenant_id == ctx.tenant_id,
            RagPipeline.name == model,
            RagPipeline.is_active == True,  # noqa: E712
        )
    )
    if pipeline is None:
        logger.warning("pipeline_not_found", model=model, tenant_id=str(ctx.tenant_id))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MODEL_NOT_FOUND", "model": model},
        )

    accessible = set(ctx.allowed_collection_ids)
    for cid in pipeline.collection_ids:
        if cid not in accessible:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "COLLECTION_ACCESS_DENIED", "collection_id": str(cid)},
            )

    return pipeline
