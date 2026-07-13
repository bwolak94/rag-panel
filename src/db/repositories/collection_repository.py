"""Repository for Collection CRUD."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError
from src.db.models.collection import Collection
from src.db.models.document import Document
from src.db.models.models_registry import ModelsRegistry


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, collection_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Collection | None:
        """Always filters by tenant_id — never returns cross-tenant data."""
        q = select(Collection).where(
            Collection.id == collection_id,
            Collection.tenant_id == tenant_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: uuid.UUID,
        allowed_ids: frozenset[uuid.UUID],
        *,
        include_inactive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Collection], int]:
        """Return (items, total_count). Scoped to allowed_ids from UserContext."""
        if not allowed_ids:
            return [], 0

        filters = [
            Collection.tenant_id == tenant_id,
            Collection.id.in_(list(allowed_ids)),
        ]
        if not include_inactive:
            filters.append(Collection.is_active.is_(True))

        base_filter = and_(*filters)
        q = select(Collection).where(base_filter).offset(offset).limit(limit)
        count_q = select(func.count()).select_from(Collection).where(base_filter)

        items = list((await self._session.execute(q)).scalars().all())
        total = (await self._session.execute(count_q)).scalar_one()
        return items, total

    async def create(
        self,
        tenant_id: uuid.UUID,
        name: str,
        description: str | None,
        embedding_model_id: uuid.UUID,
        chunk_config: dict[str, Any],
        validation_config: dict[str, Any],
    ) -> Collection:
        # Verify embedding model exists and is type='embedding'
        model_q = select(ModelsRegistry).where(
            ModelsRegistry.id == embedding_model_id,
            ModelsRegistry.type == "embedding",
            ModelsRegistry.is_active.is_(True),
        )
        model = (await self._session.execute(model_q)).scalar_one_or_none()
        if model is None:
            raise NotFoundError(
                "Embedding model not found or is not an active embedding model"
            )

        # Enforce name uniqueness within tenant
        name_q = select(Collection).where(
            Collection.tenant_id == tenant_id,
            Collection.name == name,
        )
        if (await self._session.execute(name_q)).scalar_one_or_none() is not None:
            raise ConflictError(f"Collection '{name}' already exists in this tenant")

        collection = Collection(
            tenant_id=tenant_id,
            name=name,
            description=description,
            embedding_model_id=embedding_model_id,
            chunk_config=chunk_config,
            validation_config=validation_config,
            is_active=True,
        )
        self._session.add(collection)
        await self._session.flush()
        return collection

    async def update(
        self,
        collection: Collection,
        *,
        name: str | None = None,
        description: str | None = None,
        chunk_config: dict[str, Any] | None = None,
        validation_config: dict[str, Any] | None = None,
        is_active: bool | None = None,
    ) -> Collection:
        if name is not None:
            # Check uniqueness when renaming
            name_q = select(Collection).where(
                Collection.tenant_id == collection.tenant_id,
                Collection.name == name,
                Collection.id != collection.id,
            )
            if (await self._session.execute(name_q)).scalar_one_or_none() is not None:
                raise ConflictError(
                    f"Collection '{name}' already exists in this tenant"
                )
            collection.name = name
        if description is not None:
            collection.description = description
        if chunk_config is not None:
            collection.chunk_config = chunk_config
        if validation_config is not None:
            collection.validation_config = validation_config
        if is_active is not None:
            collection.is_active = is_active
        await self._session.flush()
        return collection

    async def count_documents(
        self, collection_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> int:
        q = select(func.count()).select_from(Document).where(
            Document.collection_id == collection_id,
            Document.tenant_id == tenant_id,
            Document.status != "deleted",
        )
        return (await self._session.execute(q)).scalar_one()

    async def soft_delete(self, collection: Collection) -> None:
        """Mark is_active=False. Async DeletionService handles Qdrant/MinIO cleanup."""
        collection.is_active = False
        await self._session.flush()
