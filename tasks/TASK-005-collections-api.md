# TASK-005: Collections API

**Status:** TODO
**Priority:** P1 — required before document upload (TASK-006)
**Owner:** backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/data-model.md` §2.2 | `docs/architecture.md` §3, §9, ADR-1, ADR-6
**Estimated effort:** 3–4 days

---

## Overview

Implement CRUD endpoints for document collections. Each collection is a logical grouping of documents within a tenant, tied to a specific embedding model and chunking strategy. Creating a collection also provisions the corresponding Qdrant collection (or reuses an existing one if the same embedding model is already in use). Deleting a collection triggers a cascade that removes Qdrant points, MinIO objects, and Postgres rows for all documents in the collection.

The collection's `chunk_config` JSONB field is validated by a Pydantic model on write. The `embedding_model_id` references `models_registry` — only `type='embedding'` models are accepted. Status lifecycle: `active` → `archived` (soft-disable, no new uploads) or `reindexing` (embedding model change in progress).

## Usage

Collections are created by Contributor+ users within a tenant. The embedding model is chosen at creation time and is immutable once documents exist (requires full reindexing — see `/reindex-collection` skill). Collections are referenced by:
- `POST /documents` — upload target
- RAG pipelines (`rag_pipelines.collection_ids`) — defines retrieval scope
- `RetrievalService` — filters Qdrant queries by `collection_id IN allowed_ids`

**Minimum role for write operations:** Contributor (has `admin:collections` permission).
**Minimum role for read:** Viewer (has `documents:read` permission).

## Tech Stack

- **FastAPI** — `APIRouter(prefix="/collections")`
- **SQLAlchemy 2.x** — async ORM queries
- **Pydantic v2** — request/response schemas with `ChunkConfig` nested validation
- **`RetrievalService.ensure_collection()`** — collection provisioning delegated to `RetrievalService`; no direct `AsyncQdrantClient` usage in this task
- **TASK-003 auth dependencies** — `get_current_ctx`, `require_permission`, `assert_tenant_owns_resource`
- **TASK-004 `AuditService`** — audit log for create/update/delete

## Database Patterns

### Tables Used

- `collections` — CRUD
- `collection_access` — role-to-collection access grants (created automatically on collection creation)
- `models_registry` — FK validation for `embedding_model_id`
- `documents` — count check before deletion
- `chunks_registry` — cascade deletion (via `document_id → collection_id` join)
- `audit_log` — write operations

### Collection Repository

```python
# src/db/repositories/collection_repository.py
import uuid
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Collection, CollectionAccess, Document, Role, ModelsRegistry
from src.core.exceptions import ConflictError, NotFoundError, ValidationError


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, collection_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Collection | None:
        q = select(Collection).where(
            Collection.id == collection_id,
            Collection.tenant_id == tenant_id,  # Tenant filter always applied
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
        """Returns (items, total_count). Scoped to allowed_ids from ctx."""
        filters = [
            Collection.tenant_id == tenant_id,
            Collection.id.in_(allowed_ids),
        ]
        if not include_inactive:
            filters.append(Collection.is_active == True)

        q = select(Collection).where(and_(*filters)).offset(offset).limit(limit)
        count_q = select(func.count()).select_from(Collection).where(and_(*filters))
        items = list((await self._session.execute(q)).scalars().all())
        total = (await self._session.execute(count_q)).scalar_one()
        return items, total

    async def create(
        self,
        tenant_id: uuid.UUID,
        name: str,
        description: str | None,
        embedding_model_id: uuid.UUID,
        chunk_config: dict,
        validation_config: dict,
    ) -> Collection:
        # Verify embedding model exists and is of type 'embedding'
        model_q = select(ModelsRegistry).where(
            ModelsRegistry.id == embedding_model_id,
            ModelsRegistry.type == "embedding",
            ModelsRegistry.is_active == True,
        )
        model = (await self._session.execute(model_q)).scalar_one_or_none()
        if model is None:
            raise ValidationError(
                "embedding_model_id must reference an active embedding model"
            )

        # Check name uniqueness within tenant
        name_q = select(Collection).where(
            Collection.tenant_id == tenant_id, Collection.name == name
        )
        if (await self._session.execute(name_q)).scalar_one_or_none():
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
        chunk_config: dict | None = None,
        validation_config: dict | None = None,
        is_active: bool | None = None,
    ) -> Collection:
        if name is not None:
            collection.name = name  # type: ignore[assignment]
        if description is not None:
            collection.description = description  # type: ignore[assignment]
        if chunk_config is not None:
            collection.chunk_config = chunk_config  # type: ignore[assignment]
        if validation_config is not None:
            collection.validation_config = validation_config  # type: ignore[assignment]
        if is_active is not None:
            collection.is_active = is_active  # type: ignore[assignment]
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
        """Mark inactive. Actual Qdrant/MinIO deletion is async (DeletionService)."""
        collection.is_active = False  # type: ignore[assignment]
        await self._session.flush()
```

### Qdrant Collection Provisioning

The `CollectionService.create_collection()` method calls `RetrievalService.ensure_collection(embedding_model_slug, vector_size)` which is the ONLY method allowed to create Qdrant collections. No `AsyncQdrantClient` import exists in `src/core/clients/`.

`RetrievalService` is injected into `CollectionService` via dependency injection (see Service Layer below). The collection name produced inside `ensure_collection()` follows the canonical naming convention: `emb_{embedding_model_slug}` (see TASK-009).

## API Contracts

### Schemas

```python
# src/api/schemas/collection.py
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class ChunkConfig(BaseModel):
    """Validated schema for collections.chunk_config JSONB."""
    strategy: Literal["recursive", "sentence", "semantic", "by_section"] = "recursive"
    chunk_size: int = Field(default=512, ge=64, le=4096)
    overlap: int = Field(default=64, ge=0, le=512)
    min_chunk_size: int = Field(default=64, ge=32, le=256)
    separators: list[str] = Field(default_factory=lambda: ["\n\n", "\n", " "])
    document_type_overrides: dict[str, "ChunkConfig"] = Field(default_factory=dict)

    @model_validator(mode="after")
    def overlap_less_than_chunk_size(self) -> "ChunkConfig":
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        return self


class ValidationConfig(BaseModel):
    """Validated schema for collections.validation_config JSONB."""
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    require_review: bool = False
    pii_action: Literal["block", "flag", "allow", "review"] = "flag"
    # "review" causes the ingest graph to set documents.status = "needs_review"
    # and halt the pipeline pending admin approval.


class CollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    embedding_model_id: uuid.UUID
    chunk_config: ChunkConfig = Field(default_factory=ChunkConfig)
    validation_config: ValidationConfig = Field(default_factory=ValidationConfig)


class CollectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    chunk_config: ChunkConfig | None = None
    validation_config: ValidationConfig | None = None
    is_active: bool | None = None


class EmbeddingModelRef(BaseModel):
    id: uuid.UUID
    name: str
    model_id: str
    provider: str
    params: dict

    model_config = {"from_attributes": True}


class CollectionResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    embedding_model: EmbeddingModelRef
    chunk_config: ChunkConfig
    validation_config: ValidationConfig
    is_active: bool
    document_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CollectionListResponse(BaseModel):
    items: list[CollectionResponse]
    total: int
    page: int
    page_size: int
```

### Endpoints

```
POST   /collections              → 201 CollectionResponse
GET    /collections              → 200 CollectionListResponse
GET    /collections/{id}         → 200 CollectionResponse
PATCH  /collections/{id}         → 200 CollectionResponse
DELETE /collections/{id}         → 204
```

#### `POST /collections`

- **Permission:** `admin:collections`
- **Body:** `CollectionCreate`
- **Response 201:** `CollectionResponse`
- **422:** invalid `chunk_config` (e.g., `overlap >= chunk_size`)
- **404:** `embedding_model_id` not found or not an embedding model
- **409:** collection name already exists in tenant
- **Side effect:** calls `retrieval_service.ensure_collection(model_slug, vector_size)` — RetrievalService owns this operation
- **Audit log action:** `collection.created`

#### `GET /collections`

- **Permission:** `documents:read` (minimum: Viewer)
- **Response 200:** `CollectionListResponse` — filtered to `ctx.allowed_collection_ids`
- **Query params:** `page` (default 1), `page_size` (default 20, max 100), `include_inactive` (bool, default false; requires `admin:collections`)
- **Important:** List is scoped to `allowed_collection_ids` from `UserContext` — a Viewer only sees collections their role grants access to

#### `GET /collections/{id}`

- **Permission:** `documents:read`
- **Response 200:** `CollectionResponse` with `document_count`
- **403:** `collection_id` not in `ctx.allowed_collection_ids`
- **404:** collection not found in tenant

#### `PATCH /collections/{id}`

- **Permission:** `admin:collections`
- **Body:** `CollectionUpdate` (all fields optional)
- **Response 200:** `CollectionResponse`
- **Constraint:** `embedding_model_id` is NOT patchable — changing the embedding model requires a reindex operation (separate skill)
- **Audit log action:** `collection.updated`

#### `DELETE /collections/{id}`

- **Permission:** `admin:collections`
- **Response 204:** No content
- **Behavior:** soft-delete only in this endpoint (`is_active = False`). Async `DeletionService` handles Qdrant point removal and MinIO cleanup as a background task.
- **Audit log action:** `collection.deleted`

## Architecture — SOLID & DRY

### Service Layer

```python
# src/domain/collection_service.py
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.auth import UserContext
from src.api.schemas.collection import ChunkConfig, CollectionCreate, CollectionResponse, CollectionUpdate
from src.core.exceptions import NotFoundError, PermissionDeniedError
from src.db.repositories.collection_repository import CollectionRepository
from src.db.repositories.model_repository import ModelRepository
from src.domain.audit_service import AuditService
from src.retrieval.service import RetrievalService


class CollectionService:
    def __init__(self, session: AsyncSession, retrieval_service: RetrievalService) -> None:
        self._repo = CollectionRepository(session)
        self._audit = AuditService(session)
        self._retrieval = retrieval_service
        self._model_repo = ModelRepository(session)

    async def create_collection(
        self, body: CollectionCreate, ctx: UserContext, ip: str | None
    ) -> CollectionResponse:
        # 1. Create Postgres record (validates embedding_model_id)
        collection = await self._repo.create(
            tenant_id=ctx.tenant_id,
            name=body.name,
            description=body.description,
            embedding_model_id=body.embedding_model_id,
            chunk_config=body.chunk_config.model_dump(),
            validation_config=body.validation_config.model_dump(),
        )

        # 2. Ensure Qdrant collection exists for this embedding model.
        # RetrievalService.ensure_collection() is the only method allowed to create
        # Qdrant collections — call it here, never instantiate AsyncQdrantClient directly.
        model = await self._model_repo.get_by_id(body.embedding_model_id)
        vector_size = model.params.get("dimensions", 1024)
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

    async def _build_response(self, collection: Collection) -> CollectionResponse:
        """Load embedding model and document count, build response schema."""
        # ... load model from models_registry, count docs, assemble CollectionResponse
        ...
```

### What NOT to Do

- Do NOT instantiate `AsyncQdrantClient` in the router, the collection repository, or anywhere outside `src/retrieval/`. Collection provisioning is delegated to `RetrievalService.ensure_collection()`. Qdrant access for vector queries ONLY in `src/retrieval/service.py`.
- Do NOT allow `embedding_model_id` to be changed via `PATCH /collections/{id}` — changing the model would corrupt the collection. Validate and reject with `422` and a clear message.
- Do NOT cascade-delete documents synchronously on `DELETE /collections/{id}` — this could block for minutes. Use soft-delete + background job.
- Do NOT return all collections from `GET /collections` without filtering to `ctx.allowed_collection_ids`. This would leak collection names across roles.

## Implementation Steps

1. **Create `src/api/schemas/collection.py`** — all schemas including `ChunkConfig` with validators

2. **Call `retrieval_service.ensure_collection(model_slug, vector_size)`** — RetrievalService owns this operation; inject `RetrievalService` into `CollectionService.__init__`

3. **Create `src/db/repositories/collection_repository.py`** — full repository

4. **Create `src/db/repositories/model_repository.py`** — basic get by ID for `ModelsRegistry`

5. **Create `src/domain/collection_service.py`** — service layer

6. **Create `src/api/routers/collections.py`** — all 5 endpoint handlers

7. **Register router in `src/main.py`**

8. **Update `docs/03-Specyfikacja-API.md`** with all 5 endpoints (per hard rule: new endpoint = docs update)

9. **Write tests** (see Tests section)

10. **Run:** `ruff check --fix . && mypy src/ && pytest -x -q tests/unit/test_collections_api.py`

## Security Checklist

- Every `GET /collections/{id}` call checks `collection_id in ctx.allowed_collection_ids` (collection-level RBAC, not just endpoint-level)
- `GET /collections` list is filtered to `allowed_collection_ids` — users cannot enumerate collections they have no access to
- `embedding_model_id` update is blocked — prevents hash-model mismatch in Qdrant (which would silently break retrieval)
- Collection deletion is audited; actual data removal is tracked by `DeletionService`
- `RetrievalService.ensure_collection()` is idempotent — safe to call on every create
- Qdrant payload indexes for `tenant_id` and `collection_id` are created inside `RetrievalService.ensure_collection()` when the collection is provisioned (ensures query performance for the mandatory filter)
- No cross-tenant collection access: `get_by_id()` always includes `AND tenant_id = :tenant_id`

## Terms of Use (relevant constraints)

- Per ADR-1: collection naming in Qdrant is `emb_{embedding_model_slug}` — NOT per tenant. This is enforced by `RetrievalService.ensure_collection()`. Do not add per-tenant Qdrant collections.
- Per ADR-6: BGE-M3 is the default embedding model. The collection's `embedding_model_id` must point to an active BGE-M3 (or other registered) entry in `models_registry`.
- `chunk_config.document_type_overrides` allows per-file-type strategy — e.g., different chunk size for PDFs vs Markdown. This is validated at write time but only consumed by the ingest graph.

## Tests

References TASK-016 `tests/integration/test_api_contracts.py`.

**`tests/unit/test_chunk_config_validation.py`**

- `chunk_size=512, overlap=64` → valid
- `chunk_size=100, overlap=100` → 422 (overlap >= chunk_size)
- `chunk_size=64, overlap=0` → valid (minimum)
- `chunk_size=5000` → 422 (exceeds max 4096)
- `strategy="invalid"` → 422
- `pii_action="block"` → valid
- `document_type_overrides` with nested ChunkConfig → valid

**`tests/unit/test_collections_api.py`**

- `POST /collections` valid body → 201 with Qdrant collection provisioned (mocked)
- `POST /collections` with invalid `embedding_model_id` (LLM model, not embedding) → 422
- `POST /collections` with duplicate name → 409
- `POST /collections` as Viewer → 403
- `GET /collections` as Viewer → 200, filtered to allowed_collection_ids
- `GET /collections` as Admin with `include_inactive=true` → includes inactive
- `GET /collections/{id}` within allowed_ids → 200
- `GET /collections/{id}` NOT in allowed_ids → 403
- `PATCH /collections/{id}` valid update → 200
- `PATCH /collections/{id}` attempting to change `embedding_model_id` → 422 with message
- `DELETE /collections/{id}` → 204, collection `is_active=False`
- `DELETE /collections/{id}` as Contributor (lacks `admin:collections`) → 403

**`tests/security/test_collection_scoping.py`** (mark: `@pytest.mark.tenant_isolation`)

- User in tenant A cannot list tenant B's collections
- User with Viewer role cannot see collections not in their `allowed_collection_ids`
- Qdrant provisioning uses shared collection name (not per-tenant) — verified that `RetrievalService.ensure_collection()` is called with model slug, not tenant ID, and produces `emb_{model_slug}` collection name

**Authorization matrix:**

| Endpoint | Admin | Contributor | Viewer |
|---|---|---|---|
| `POST /collections` | 201 | 403 | 403 |
| `GET /collections` | 200 | 200 | 200 (filtered) |
| `GET /collections/{id}` | 200 | 200 | 200 (if in allowed) |
| `PATCH /collections/{id}` | 200 | 403 | 403 |
| `DELETE /collections/{id}` | 204 | 403 | 403 |

## Definition of Done

- [ ] All 5 collection endpoints implemented with correct status codes and response schemas
- [ ] `ChunkConfig` Pydantic model with all validators (overlap < chunk_size, etc.)
- [ ] `RetrievalService.ensure_collection()` called on create; idempotent; payload indexes created inside `RetrievalService`
- [ ] `GET /collections` list filtered to `ctx.allowed_collection_ids`
- [ ] `embedding_model_id` update blocked in `PATCH` with clear error
- [ ] `DELETE /collections/{id}` is soft-delete; audit log entry written
- [ ] `docs/03-Specyfikacja-API.md` updated with all 5 endpoints
- [ ] `CollectionRepository.get_by_id()` always includes `tenant_id` filter
- [ ] All unit and security tests pass
- [ ] `ruff check . && mypy src/` exit zero
