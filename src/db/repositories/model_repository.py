"""Repository for ModelsRegistry lookups."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.models_registry import ModelsRegistry


class ModelRepository:
    """Data access layer for ModelsRegistry records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, model_id: uuid.UUID) -> ModelsRegistry | None:
        """Fetch a model by primary key (no tenant filter — used internally).

        Args:
            model_id: The model UUID.

        Returns:
            The matching ModelsRegistry or None.
        """
        return await self._session.get(ModelsRegistry, model_id)

    async def get_active_embedding_model(self, model_id: uuid.UUID) -> ModelsRegistry | None:
        """Return the model only if it is type='embedding' and is_active=True.

        Args:
            model_id: The model UUID.

        Returns:
            The matching ModelsRegistry or None if not active/embedding.
        """
        q = select(ModelsRegistry).where(
            ModelsRegistry.id == model_id,
            ModelsRegistry.type == "embedding",
            ModelsRegistry.is_active.is_(True),
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def list_for_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ModelsRegistry], int]:
        """Return models visible to the tenant: system-wide (tenant_id IS NULL) + tenant-private.

        Args:
            tenant_id: The tenant UUID from JWT context.
            include_inactive: When True, include models with is_active=False.
            offset: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            A tuple of (items, total_count).
        """
        base = select(ModelsRegistry).where(
            or_(ModelsRegistry.tenant_id.is_(None), ModelsRegistry.tenant_id == tenant_id)
        )
        if not include_inactive:
            base = base.where(ModelsRegistry.is_active.is_(True))
        count_q = select(func.count()).select_from(base.subquery())
        total: int = (await self._session.execute(count_q)).scalar_one()
        items_q = base.order_by(ModelsRegistry.created_at.desc()).offset(offset).limit(limit)
        items = list((await self._session.execute(items_q)).scalars().all())
        return items, total

    async def create(self, model: ModelsRegistry) -> ModelsRegistry:
        """Persist a new model entry and flush to obtain the generated ID.

        Args:
            model: The ModelsRegistry instance to persist.

        Returns:
            The flushed ModelsRegistry with a populated ID.
        """
        self._session.add(model)
        await self._session.flush()
        return model

    async def count_referencing_pipelines(self, model_id: uuid.UUID) -> int:
        """Count active pipelines referencing the given model.

        Used before deactivating a model to guard against breaking active pipelines.

        Args:
            model_id: The model UUID to check.

        Returns:
            The number of active pipelines referencing this model.
        """
        from src.db.models.rag_pipeline import RagPipeline

        q = select(func.count()).where(
            RagPipeline.llm_model_id == model_id,
            RagPipeline.is_active.is_(True),
        )
        return (await self._session.execute(q)).scalar_one()
