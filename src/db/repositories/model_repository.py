"""Repository for ModelsRegistry lookups."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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

    async def get_visible_by_id(
        self, model_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> ModelsRegistry | None:
        """Fetch a model visible to the tenant: system-wide (tenant_id IS NULL) OR tenant-owned.

        Use this for all service-layer lookups that act on behalf of a specific tenant.
        The unscoped get_by_id() remains for internal system use only.

        Args:
            model_id: The model UUID to retrieve.
            tenant_id: The tenant UUID from JWT context.

        Returns:
            The matching ModelsRegistry or None if not visible to this tenant.
        """
        q = select(ModelsRegistry).where(
            ModelsRegistry.id == model_id,
            or_(ModelsRegistry.tenant_id.is_(None), ModelsRegistry.tenant_id == tenant_id),
        )
        return (await self._session.execute(q)).scalar_one_or_none()

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

    async def update_calibration(
        self,
        model_id: uuid.UUID,
        threshold: float,
        sample_count: int,
    ) -> ModelsRegistry | None:
        """Write calibration fields to a model record and flush.

        Callers must NOT call session.commit() — that is the router's responsibility.

        Args:
            model_id: Primary key of the ModelsRegistry row to update.
            threshold: Calibrated score threshold (0.0–1.0).
            sample_count: Number of eval samples that produced this threshold.

        Returns:
            The updated ModelsRegistry instance, or None if not found.
        """
        model = await self._session.get(ModelsRegistry, model_id)
        if model is None:
            return None
        model.score_threshold_calibrated = threshold
        model.threshold_calibrated_at = datetime.now(tz=UTC)
        model.threshold_calibration_samples = sample_count
        await self._session.flush()
        return model

    async def get_calibrated_threshold(self, model_id: uuid.UUID) -> float | None:
        """Return score_threshold_calibrated for a model, or None if not calibrated.

        Args:
            model_id: Primary key of the ModelsRegistry row.

        Returns:
            The calibrated threshold float, or None.
        """
        q = select(ModelsRegistry.score_threshold_calibrated).where(ModelsRegistry.id == model_id)
        return (await self._session.execute(q)).scalar_one_or_none()
