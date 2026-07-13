"""Repository for ModelsRegistry lookups."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.models_registry import ModelsRegistry


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, model_id: uuid.UUID) -> ModelsRegistry | None:
        return await self._session.get(ModelsRegistry, model_id)

    async def get_active_embedding_model(self, model_id: uuid.UUID) -> ModelsRegistry | None:
        """Return the model only if it is type='embedding' and is_active=True."""
        q = select(ModelsRegistry).where(
            ModelsRegistry.id == model_id,
            ModelsRegistry.type == "embedding",
            ModelsRegistry.is_active.is_(True),
        )
        return (await self._session.execute(q)).scalar_one_or_none()
