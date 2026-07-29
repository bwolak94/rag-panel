"""Repository for RagPipeline CRUD operations."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.rag_pipeline import RagPipeline


class PipelineRepository:
    """Data access layer for RagPipeline records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, pipeline_id: uuid.UUID, tenant_id: uuid.UUID) -> RagPipeline | None:
        """Fetch a pipeline by ID scoped to the given tenant.

        Args:
            pipeline_id: The pipeline UUID.
            tenant_id: The tenant UUID from JWT context.

        Returns:
            The matching RagPipeline or None if not found.
        """
        q = select(RagPipeline).where(
            RagPipeline.id == pipeline_id,
            RagPipeline.tenant_id == tenant_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[RagPipeline], int]:
        """Return paginated pipelines for a given tenant.

        Args:
            tenant_id: The tenant UUID from JWT context.
            include_inactive: When True, include pipelines with is_active=False.
            offset: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            A tuple of (items, total_count).
        """
        base = select(RagPipeline).where(RagPipeline.tenant_id == tenant_id)
        if not include_inactive:
            base = base.where(RagPipeline.is_active.is_(True))
        count_q = select(func.count()).select_from(base.subquery())
        total: int = (await self._session.execute(count_q)).scalar_one()
        items_q = base.order_by(RagPipeline.created_at.desc()).offset(offset).limit(limit)
        items = list((await self._session.execute(items_q)).scalars().all())
        return items, total

    async def create(self, pipeline: RagPipeline) -> RagPipeline:
        """Persist a new pipeline and flush to obtain the generated ID.

        Args:
            pipeline: The RagPipeline instance to persist.

        Returns:
            The flushed RagPipeline with a populated ID.
        """
        self._session.add(pipeline)
        await self._session.flush()
        return pipeline

    async def count_referencing_model(self, model_id: uuid.UUID) -> int:
        """Count active pipelines referencing the given model.

        Used before deactivating a model to guard against breaking active pipelines.

        Args:
            model_id: The model UUID to check.

        Returns:
            The number of active pipelines referencing this model.
        """
        q = select(func.count()).where(
            RagPipeline.llm_model_id == model_id,
            RagPipeline.is_active.is_(True),
        )
        return (await self._session.execute(q)).scalar_one()

    async def delete(self, pipeline: RagPipeline) -> None:
        """Hard-delete a pipeline record and flush.

        Args:
            pipeline: The RagPipeline instance to delete.
        """
        await self._session.delete(pipeline)
        await self._session.flush()
