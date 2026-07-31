"""Repository for Collection CRUD."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import ConflictError, NotFoundError
from src.db.models.collection import Collection
from src.db.models.document import Document
from src.db.models.models_registry import ModelsRegistry


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, collection_id: uuid.UUID, tenant_id: uuid.UUID) -> Collection | None:
        """Return collection if it belongs to the tenant OR is a public collection.

        Public collections are readable by every tenant. Callers must enforce
        write restrictions separately (RetrievalService.upsert_batch /
        delete_by_document check managed_by_tenant_id).
        """
        q = select(Collection).where(
            Collection.id == collection_id,
            or_(
                Collection.tenant_id == tenant_id,
                Collection.is_public.is_(True),
            ),
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
            raise NotFoundError("Embedding model not found or is not an active embedding model")

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
                raise ConflictError(f"Collection '{name}' already exists in this tenant")
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

    async def count_documents(self, collection_id: uuid.UUID, tenant_id: uuid.UUID) -> int:
        q = (
            select(func.count())
            .select_from(Document)
            .where(
                Document.collection_id == collection_id,
                Document.tenant_id == tenant_id,
                Document.status != "deleted",
            )
        )
        return (await self._session.execute(q)).scalar_one()

    async def soft_delete(self, collection: Collection) -> None:
        """Mark is_active=False. Async DeletionService handles Qdrant/MinIO cleanup."""
        collection.is_active = False
        await self._session.flush()

    async def get_public_collections(self) -> list[Collection]:
        """Return all active public collections regardless of tenant.

        These collections are readable by every tenant. This method has NO
        tenant_id parameter by design — it is the one intentional cross-tenant
        read that returns only platform-wide shared data.

        Returns:
            List of Collection with is_public=True and is_active=True.
        """
        q = select(Collection).where(
            Collection.is_public.is_(True),
            Collection.is_active.is_(True),
        )
        return list((await self._session.execute(q)).scalars().all())

    async def get_accessible_collections(
        self,
        tenant_id: uuid.UUID,
        *,
        include_inactive: bool = False,
    ) -> list[Collection]:
        """Return all collections accessible to a tenant: own + all public.

        Used when building TenantContext.public_collection_ids for retrieval.

        Args:
            tenant_id: The calling tenant's UUID.
            include_inactive: When False (default), only active collections are
                returned. Pass True for admin tooling.

        Returns:
            Union of the tenant's own collections and all public collections.
            No duplicates — a collection owned by the tenant that also has
            is_public=True appears only once.
        """
        base_filter = or_(
            Collection.tenant_id == tenant_id,
            Collection.is_public.is_(True),
        )
        filters = [base_filter]
        if not include_inactive:
            filters.append(Collection.is_active.is_(True))

        q = select(Collection).where(and_(*filters))
        return list((await self._session.execute(q)).scalars().all())
