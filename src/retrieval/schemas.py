"""Retrieval service data transfer objects."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field


class TenantContext(BaseModel):
    """Tenant scope for all Qdrant operations. Always from JWT — never from request body."""

    tenant_id: UUID
    allowed_collection_ids: list[UUID] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    """Result returned from a Qdrant similarity search."""

    point_id: UUID
    document_id: UUID
    chunk_id: UUID | None = None
    score: float
    payload: dict[str, Any]
    page_number: int | None = None
    highlight_text: str | None = None
    collection_id: UUID | None = None


class QdrantPoint(BaseModel):
    """Single Qdrant vector point ready for upsert.

    payload MUST include tenant_id, collection_id, document_id.
    """

    id: UUID
    vector: list[float]
    payload: dict[str, Any]


@runtime_checkable
class RetrievalServiceProtocol(Protocol):
    """Structural protocol for RetrievalService.

    Allows other modules to depend on the abstraction rather than the concrete class,
    enabling testability and potential future alternative implementations.
    """

    async def search(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        top_k: int,
        score_threshold: float,
        additional_filter: object | None,
    ) -> list[RetrievalResult]: ...

    async def upsert_batch(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        points: list[QdrantPoint],
    ) -> None: ...

    async def delete_by_document(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        document_id: UUID,
    ) -> int: ...

    async def delete_by_tenant(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
    ) -> int: ...

    async def ensure_collection(
        self,
        embedding_model_slug: str,
        vector_size: int,
        distance: str,
    ) -> None: ...
