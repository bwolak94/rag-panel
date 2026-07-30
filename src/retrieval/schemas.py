"""Retrieval service data transfer objects."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, Field


class SearchMode(StrEnum):
    """Search strategy for a retrieval call.

    DENSE  — pure vector similarity search (default).
    HYBRID — BM25 keyword search fused with dense search via Reciprocal Rank Fusion.
             Typically adds 5–15% MRR over DENSE alone.
    """

    DENSE = "dense"
    HYBRID = "hybrid"


class TenantContext(BaseModel):
    """Tenant scope for all Qdrant operations. Always from JWT — never from request body.

    Read filter semantics (enforced in filters.build_read_filter):
        (tenant_id == self.tenant_id AND collection_id IN allowed_collection_ids)
        OR (collection_id IN public_collection_ids)

    Write filter semantics (enforced in RetrievalService.upsert_batch /
    delete_by_document):
        tenant_id == self.tenant_id — public collections require the caller
        to be the managed_by_tenant_id; RetrievalService rejects all other
        write attempts with PermissionError.
    """

    tenant_id: UUID
    allowed_collection_ids: list[UUID] = Field(default_factory=list)
    # IDs of platform-wide public collections the tenant may READ but not write.
    # Populated by the auth dependency from CollectionRepository.get_public_collections().
    public_collection_ids: list[UUID] = Field(default_factory=list)


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
        search_mode: SearchMode,
        query_text: str | None,
    ) -> list[RetrievalResult]: ...

    async def search_hybrid(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        query_text: str,
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
