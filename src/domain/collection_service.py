"""CollectionService — business logic for collection management.

Orchestrates CollectionRepository, ModelRepository, AuditService,
and RetrievalService.ensure_collection().
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.collection import (
    ChunkConfig,
    CollectionCreate,
    CollectionListResponse,
    CollectionResponse,
    CollectionUpdate,
    EmbeddingModelRef,
    ValidationConfig,
)
from src.core.exceptions import NotFoundError
from src.db.repositories.collection_repository import CollectionRepository
from src.db.repositories.model_repository import ModelRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext, require_collection_read
from src.retrieval.service import RetrievalService


class CollectionService:
    def __init__(self, session: AsyncSession, retrieval_service: RetrievalService) -> None:
        self._repo = CollectionRepository(session)
        self._model_repo = ModelRepository(session)
        self._audit = AuditService(session)
        self._retrieval = retrieval_service

    async def create_collection(
        self, body: CollectionCreate, ctx: UserContext, ip: str | None
    ) -> CollectionResponse:
        # 1. Create Postgres record (validates embedding_model_id, name uniqueness)
        collection = await self._repo.create(
            tenant_id=ctx.tenant_id,
            name=body.name,
            description=body.description,
            embedding_model_id=body.embedding_model_id,
            chunk_config=body.chunk_config.model_dump(),
            validation_config=body.validation_config.model_dump(),
        )

        # 2. Provision Qdrant collection — RetrievalService owns this operation
        model = await self._model_repo.get_by_id(body.embedding_model_id)
        if model is None:
            raise NotFoundError(f"Embedding model {body.embedding_model_id} not found")
        vector_size: int = model.params.get("dimensions", 1024)
        model_slug = model.name.lower().replace("-", "_").replace(" ", "_")
        await self._retrieval.ensure_collection(model_slug, vector_size)

        # 3. Audit
        await self._audit.log(
            ctx=ctx,
            action="collection.created",
            resource_type="collection",
            resource_id=collection.id,
            details={"name": collection.name, "embedding_model_id": str(body.embedding_model_id)},
            ip=ip,
        )

        return await self._build_response(collection)

    async def list_collections(
        self,
        ctx: UserContext,
        *,
        include_inactive: bool,
        offset: int,
        limit: int,
        page: int,
        page_size: int,
    ) -> CollectionListResponse:
        items, total = await self._repo.list_by_tenant(
            tenant_id=ctx.tenant_id,
            allowed_ids=ctx.allowed_collection_ids,
            include_inactive=include_inactive,
            offset=offset,
            limit=limit,
        )
        responses = [await self._build_response(c) for c in items]
        return CollectionListResponse(
            items=responses,
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_collection(
        self, collection_id: uuid.UUID, ctx: UserContext
    ) -> CollectionResponse:
        # Collection-level RBAC check before hitting DB
        require_collection_read(collection_id, ctx)

        collection = await self._repo.get_by_id(collection_id, ctx.tenant_id)
        if collection is None:
            raise NotFoundError(f"Collection {collection_id} not found")

        return await self._build_response(collection)

    async def update_collection(
        self,
        collection_id: uuid.UUID,
        body: CollectionUpdate,
        ctx: UserContext,
        ip: str | None,
    ) -> CollectionResponse:
        collection = await self._repo.get_by_id(collection_id, ctx.tenant_id)
        if collection is None:
            raise NotFoundError(f"Collection {collection_id} not found")

        collection = await self._repo.update(
            collection,
            name=body.name,
            description=body.description,
            chunk_config=body.chunk_config.model_dump() if body.chunk_config is not None else None,
            validation_config=(
                body.validation_config.model_dump()
                if body.validation_config is not None
                else None
            ),
            is_active=body.is_active,
        )

        await self._audit.log(
            ctx=ctx,
            action="collection.updated",
            resource_type="collection",
            resource_id=collection_id,
            details={"fields": list(body.model_fields_set)},
            ip=ip,
        )

        return await self._build_response(collection)

    async def delete_collection(
        self, collection_id: uuid.UUID, ctx: UserContext, ip: str | None
    ) -> None:
        collection = await self._repo.get_by_id(collection_id, ctx.tenant_id)
        if collection is None:
            raise NotFoundError(f"Collection {collection_id} not found")

        await self._repo.soft_delete(collection)

        await self._audit.log(
            ctx=ctx,
            action="collection.deleted",
            resource_type="collection",
            resource_id=collection_id,
            details={"name": collection.name},
            ip=ip,
        )

    async def _build_response(self, collection: object) -> CollectionResponse:
        from src.db.models.collection import Collection

        assert isinstance(collection, Collection)

        model = await self._model_repo.get_by_id(collection.embedding_model_id)
        if model is None:
            raise NotFoundError(
                f"Embedding model {collection.embedding_model_id} not found in registry"
            )

        doc_count = await self._repo.count_documents(collection.id, collection.tenant_id)

        return CollectionResponse(
            id=collection.id,
            tenant_id=collection.tenant_id,
            name=collection.name,
            description=collection.description,
            embedding_model=EmbeddingModelRef(
                id=model.id,
                name=model.name,
                model_id=model.model_id,
                provider=model.provider,
                params=model.params,
            ),
            chunk_config=ChunkConfig.model_validate(collection.chunk_config),
            validation_config=ValidationConfig.model_validate(collection.validation_config),
            is_active=collection.is_active,
            document_count=doc_count,
            created_at=collection.created_at,
            updated_at=collection.updated_at,
        )
