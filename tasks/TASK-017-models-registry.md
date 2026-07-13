# TASK-017: Models Registry

**Status:** TODO
**Priority:** P1 — required by TASK-008 (ingest graph) and TASK-010 (query graph)
**Owner:** backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md` §2 (Principle 4), §12 (ADR-4, ADR-6) | `docs/data-model.md` §2.3
**Estimated effort:** 2–3 days

---

## Overview

Implement the `models_registry` table as the single source of truth for all LLM and embedding model
configurations. Every LangGraph node, the ingest pipeline, and `RetrievalService` must retrieve
model configuration from `ModelRegistryService` — never from hardcoded strings, inline config dicts,
or raw environment variables.

The table already exists in the Postgres schema (created by TASK-002). This task implements:

1. `ModelRepository` — async SQLAlchemy queries with tenant-access guard.
2. `ModelRegistryService` — domain service wrapping the repository; builds OpenAI-compatible
   async clients; handles Redis caching with TTL 300s.
3. REST API (`/models` prefix) — six endpoints with RBAC; Admin+ for write operations.
4. Seed function — loads default models from `seed_models.json` at application startup via the
   lifespan event defined in TASK-001.
5. Cross-references — updates TASK-008 and TASK-010 to call `ModelRegistryService` instead of
   reading `LLM_BASE_URL` / `EMBEDDING_BASE_URL` directly from `settings`.

`docs/architecture.md` §2 Principle 4 states: "All LLM and embedding calls go through an abstract
client that speaks the OpenAI API protocol. Changing a model means adding or updating a
`models_registry` entry, not changing application code." This task is the implementation of that
principle.

---

## Usage

### From ingest graph (TASK-008, node_embed)

```python
from src.domain.model_registry import ModelRegistryService
from src.retrieval.schemas import TenantContext

# Injected via FastAPI dependency or passed by LangGraph state
svc: ModelRegistryService = ...

embedding_client = await svc.get_embedding_client(
    model_id=collection.embedding_model_id,
    ctx=TenantContext(tenant_id=tenant_id, allowed_collection_ids=[]),
)

response = await embedding_client.embeddings.create(
    model=model_record.model_id,
    input=chunk_texts,
)
vectors = [item.embedding for item in response.data]
```

### From query graph (TASK-010, node_generate)

```python
llm_client = await svc.get_llm_client(
    model_id=pipeline.llm_model_id,
    ctx=ctx,
)
completion = await llm_client.chat.completions.create(
    model=model_record.model_id,
    messages=prompt_messages,
    temperature=model_record.params.get("temperature", 0.1),
    max_tokens=model_record.params.get("max_tokens", 2048),
)
```

### Health check (Admin dashboard)

```python
is_reachable: bool = await svc.validate_model_reachable(model_id=model_id)
```

---

## Tech Stack

- **openai** `1.x` — `AsyncOpenAI` as the provider-agnostic client; all Ollama, vLLM, and
  OpenAI-compatible endpoints use the same client pointed at different `base_url`. Justification:
  ADR-4 (Ollama/vLLM both expose OpenAI-compatible APIs; single client abstraction eliminates
  provider-specific code paths).
- **redis** `5.x` — async Redis client for model record caching (TTL 300s). Cache key:
  `model:{model_id}`. Serialised as JSON. Reuses the Redis connection pool from `src/core/clients.py`
  (created in TASK-001).
- **SQLAlchemy 2.0** async ORM — `AsyncSession`, mapped `ModelRegistry` ORM model (from TASK-002).
- **Pydantic v2** — request/response schemas; `ModelConfig` for the JSONB `params` field.
- **structlog** — log model_id and tenant_id only; never log endpoint URLs containing credentials
  or `api_key` values.
- **httpx** `0.27+` — used only inside `validate_model_reachable()` for the `/models` health-check
  GET; not for embeddings/completions (those go through `AsyncOpenAI`).

---

## Database Patterns

### ORM Model (already defined in TASK-002, reproduced for reference)

```python
# src/db/models/model_registry.py
import uuid
from sqlalchemy import UUID, Boolean, CheckConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from src.db.base import Base


class ModelRegistry(Base):
    __tablename__ = "models_registry"
    __table_args__ = (
        CheckConstraint("type IN ('llm', 'embedding')", name="ck_model_type"),
        CheckConstraint(
            "provider IN ('ollama', 'vllm', 'openai_compat')",
            name="ck_model_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(500), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    allowed_roles: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
```

### Tenant access rule

A model record is accessible to a `TenantContext` if and only if:

```
model.tenant_id IS NULL  OR  model.tenant_id = ctx.tenant_id
```

`model.tenant_id IS NULL` means the model is system-wide (e.g., the default BGE-M3 embedding
model registered at seed time). `model.tenant_id IS NOT NULL` means a private endpoint or
fine-tuned model registered by that tenant's Admin.

The repository enforces this filter — callers never construct the SQL predicate themselves.

### `ModelRepository` (src/db/repositories/model_repository.py)

```python
from uuid import UUID
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models.model_registry import ModelRegistry
from src.core.exceptions import NotFoundError, TenantIsolationError


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, model_id: UUID, tenant_id: UUID | None) -> ModelRegistry:
        """Load a model record and verify tenant access.

        Args:
            model_id: UUID of the model record.
            tenant_id: Caller's tenant UUID. None only for system-level operations.

        Returns:
            ModelRegistry ORM instance.

        Raises:
            NotFoundError: If the model does not exist or is not active.
            TenantIsolationError: If the model belongs to a different tenant.
        """
        stmt = select(ModelRegistry).where(ModelRegistry.id == model_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None or not row.is_active:
            raise NotFoundError(f"Model {model_id} not found or inactive")
        if tenant_id is not None and row.tenant_id is not None:
            if row.tenant_id != tenant_id:
                raise TenantIsolationError("Model belongs to a different tenant")
        return row

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        model_type: str | None = None,
    ) -> list[ModelRegistry]:
        """List models visible to a tenant: global (NULL) + tenant-specific.

        Args:
            tenant_id: Caller's tenant UUID.
            model_type: Optional filter — "llm" or "embedding".

        Returns:
            List of active ModelRegistry rows ordered by name.
        """
        stmt = (
            select(ModelRegistry)
            .where(
                ModelRegistry.is_active == True,  # noqa: E712
                or_(
                    ModelRegistry.tenant_id == None,  # noqa: E711
                    ModelRegistry.tenant_id == tenant_id,
                ),
            )
            .order_by(ModelRegistry.name)
        )
        if model_type is not None:
            stmt = stmt.where(ModelRegistry.type == model_type)
        return list((await self._session.execute(stmt)).scalars().all())

    async def create(self, data: dict) -> ModelRegistry:
        """Insert a new model record. Caller is responsible for permission check."""
        row = ModelRegistry(**data)
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return row

    async def update(self, model_id: UUID, tenant_id: UUID, data: dict) -> ModelRegistry:
        """Update mutable fields of a model. Raises TenantIsolationError on cross-tenant write."""
        row = await self.get_by_id(model_id, tenant_id)
        for key, value in data.items():
            setattr(row, key, value)
        await self._session.flush()
        return row

    async def deactivate(self, model_id: UUID, tenant_id: UUID) -> None:
        """Soft-delete: set is_active=False. Does not remove the row (FK integrity)."""
        row = await self.get_by_id(model_id, tenant_id)
        row.is_active = False
        await self._session.flush()
```

---

## API Contracts

### Pydantic schemas (src/api/schemas/model_registry.py)

```python
from uuid import UUID
from typing import Literal
from pydantic import BaseModel, HttpUrl, Field


class ModelParamsEmbedding(BaseModel):
    """JSONB params schema for embedding models."""
    model_name: str                        # provider-internal model ID
    vector_size: int = 1024
    max_tokens: int = 8192


class ModelParamsLLM(BaseModel):
    """JSONB params schema for LLM models."""
    model_name: str
    context_window: int = 8192
    max_tokens: int = 2048
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)


class ModelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: Literal["llm", "embedding"]
    provider: Literal["ollama", "vllm", "openai_compat"]
    endpoint_url: HttpUrl
    model_id: str = Field(min_length=1, max_length=255)
    params: dict = Field(default_factory=dict)
    # tenant_id is derived from JWT context, never from request body


class ModelUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    endpoint_url: HttpUrl | None = None
    params: dict | None = None
    is_active: bool | None = None


class ModelResponse(BaseModel):
    id: UUID
    tenant_id: UUID | None          # None = global model
    name: str
    type: str
    provider: str
    endpoint_url: str
    model_id: str
    params: dict
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}


class ModelListResponse(BaseModel):
    items: list[ModelResponse]
    total: int


class ModelReachableResponse(BaseModel):
    model_id: UUID
    reachable: bool
    checked_at: str
```

### Endpoint table

| Method | Path | Permission | Description |
|--------|------|-----------|-------------|
| `GET` | `/models` | `models:read` | List models visible to the tenant |
| `GET` | `/models/{id}` | `models:read` | Get single model details |
| `POST` | `/models` | `models:write` | Register new model (Admin+) |
| `PATCH` | `/models/{id}` | `models:write` | Update model config or endpoint (Admin+) |
| `DELETE` | `/models/{id}` | `models:write` | Soft-delete: set `is_active=False` (Admin+) |
| `POST` | `/models/{id}/check` | `models:write` | Validate model endpoint is reachable (Admin+) |

Note: `GET /v1/models` (OpenAI-compatible, used by Open WebUI) returns pipeline slugs, not model
registry entries — that endpoint belongs to TASK-011 and is out of scope here.

### Router (src/api/routers/models.py)

```python
from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import require, get_tenant_context
from src.api.dependencies.db import get_db_session
from src.api.schemas.model_registry import (
    ModelCreateRequest, ModelUpdateRequest, ModelResponse,
    ModelListResponse, ModelReachableResponse,
)
from src.domain.model_registry import ModelRegistryService
from src.retrieval.schemas import TenantContext

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=ModelListResponse)
async def list_models(
    type: str | None = None,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:read")),
    session: AsyncSession = Depends(get_db_session),
) -> ModelListResponse:
    svc = ModelRegistryService(session=session)
    items = await svc.list_models(ctx=ctx, model_type=type)
    return ModelListResponse(items=items, total=len(items))


@router.get("/{model_id}", response_model=ModelResponse)
async def get_model(
    model_id: UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:read")),
    session: AsyncSession = Depends(get_db_session),
) -> ModelResponse:
    svc = ModelRegistryService(session=session)
    return await svc.get_model(model_id=model_id, ctx=ctx)


@router.post("", response_model=ModelResponse, status_code=201)
async def register_model(
    body: ModelCreateRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:write")),
    session: AsyncSession = Depends(get_db_session),
) -> ModelResponse:
    svc = ModelRegistryService(session=session)
    return await svc.register_model(data=body, ctx=ctx)


@router.patch("/{model_id}", response_model=ModelResponse)
async def update_model(
    model_id: UUID,
    body: ModelUpdateRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:write")),
    session: AsyncSession = Depends(get_db_session),
) -> ModelResponse:
    svc = ModelRegistryService(session=session)
    return await svc.update_model(model_id=model_id, data=body, ctx=ctx)


@router.delete("/{model_id}", status_code=204)
async def deactivate_model(
    model_id: UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:write")),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    svc = ModelRegistryService(session=session)
    await svc.deactivate_model(model_id=model_id, ctx=ctx)


@router.post("/{model_id}/check", response_model=ModelReachableResponse)
async def check_model_reachable(
    model_id: UUID,
    ctx: TenantContext = Depends(get_tenant_context),
    _: None = Depends(require("models:write")),
    session: AsyncSession = Depends(get_db_session),
) -> ModelReachableResponse:
    svc = ModelRegistryService(session=session)
    reachable = await svc.validate_model_reachable(model_id=model_id, ctx=ctx)
    from datetime import datetime, timezone
    return ModelReachableResponse(
        model_id=model_id,
        reachable=reachable,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
```

All write endpoints write an `audit_log` entry (action, actor_id, resource_id) via the shared
`AuditLogService` from TASK-003.

---

## Architecture — SOLID & DRY

### File layout

```
src/
  domain/
    model_registry.py          # ModelRegistryService (this task)
  db/
    repositories/
      model_repository.py      # ModelRepository (this task)
    models/
      model_registry.py        # ORM model (TASK-002, verify exists)
  api/
    routers/
      models.py                # 6 endpoints (this task)
    schemas/
      model_registry.py        # Pydantic schemas (this task)
seed_models.json               # Default models for local dev (this task)
```

### ModelRegistryService (src/domain/model_registry.py)

```python
import json
from uuid import UUID
import httpx
import structlog
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError
from src.db.repositories.model_repository import ModelRepository
from src.db.models.model_registry import ModelRegistry
from src.api.schemas.model_registry import ModelCreateRequest, ModelUpdateRequest, ModelResponse
from src.retrieval.schemas import TenantContext

log = structlog.get_logger(__name__)
_CACHE_TTL = 300  # seconds


class ModelRegistryService:
    """Domain service for model registry operations.

    Provides OpenAI-compatible async clients for LLM and embedding models.
    Model records are cached in Redis (TTL 300s); cache is invalidated on write.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ModelRepository(session=session)

    async def get_model(self, model_id: UUID, ctx: TenantContext) -> ModelResponse:
        """Load a model record, check is_active, check tenant access.

        Args:
            model_id: Model registry UUID.
            ctx: Caller's tenant context (from verified JWT).

        Returns:
            ModelResponse Pydantic model.

        Raises:
            NotFoundError: If model does not exist or is inactive.
            TenantIsolationError: If model belongs to a different tenant.
        """
        record = await self._get_record_cached(model_id, ctx.tenant_id)
        return ModelResponse.model_validate(record)

    async def get_llm_client(self, model_id: UUID, ctx: TenantContext) -> AsyncOpenAI:
        """Return an OpenAI-compatible async client for an LLM model.

        Args:
            model_id: UUID of an 'llm' type record in models_registry.
            ctx: Caller's tenant context.

        Returns:
            AsyncOpenAI client pointed at the model's endpoint_url.

        Raises:
            NotFoundError: If model is missing, inactive, or not an LLM.
        """
        record = await self._get_record_cached(model_id, ctx.tenant_id)
        if record.type != "llm":
            raise NotFoundError(f"Model {model_id} is not of type 'llm'")
        return self._build_client(record)

    async def get_embedding_client(self, model_id: UUID, ctx: TenantContext) -> AsyncOpenAI:
        """Return an OpenAI-compatible async client for an embedding model.

        Args:
            model_id: UUID of an 'embedding' type record in models_registry.
            ctx: Caller's tenant context.

        Returns:
            AsyncOpenAI client pointed at the model's endpoint_url.

        Raises:
            NotFoundError: If model is missing, inactive, or not an embedding model.
        """
        record = await self._get_record_cached(model_id, ctx.tenant_id)
        if record.type != "embedding":
            raise NotFoundError(f"Model {model_id} is not of type 'embedding'")
        return self._build_client(record)

    async def list_models(
        self, ctx: TenantContext, model_type: str | None = None
    ) -> list[ModelResponse]:
        """List models visible to the caller's tenant (global + tenant-specific).

        Args:
            ctx: Caller's tenant context.
            model_type: Optional filter — "llm" or "embedding". None = both.

        Returns:
            List of ModelResponse ordered by name.
        """
        records = await self._repo.list_for_tenant(
            tenant_id=ctx.tenant_id, model_type=model_type
        )
        return [ModelResponse.model_validate(r) for r in records]

    async def validate_model_reachable(
        self, model_id: UUID, ctx: TenantContext | None = None
    ) -> bool:
        """Check if the model endpoint responds to GET /models.

        Uses httpx with a 5-second timeout. Does NOT raise on timeout — returns False.

        Args:
            model_id: Model registry UUID.
            ctx: Optional tenant context. If None, skips tenant access check (system-only call).

        Returns:
            True if the endpoint returns HTTP 2xx; False on timeout or non-2xx.
        """
        tenant_id = ctx.tenant_id if ctx is not None else None
        record = await self._get_record_cached(model_id, tenant_id)
        base_url = record.endpoint_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base_url}/models")
                return resp.is_success
        except Exception:
            log.warning("model_registry.validate_unreachable", model_id=str(model_id))
            return False

    async def register_model(
        self, data: ModelCreateRequest, ctx: TenantContext
    ) -> ModelResponse:
        """Register a new model. Scope: caller's tenant (tenant-specific) or global (system Admin).

        Writes audit_log entry for the create action.
        """
        row = await self._repo.create({
            "name": data.name,
            "type": data.type,
            "provider": data.provider,
            "endpoint_url": str(data.endpoint_url),
            "model_id": data.model_id,
            "params": data.params,
            "tenant_id": ctx.tenant_id,
        })
        log.info("model_registry.registered", model_id=str(row.id), tenant_id=str(ctx.tenant_id))
        return ModelResponse.model_validate(row)

    async def update_model(
        self, model_id: UUID, data: ModelUpdateRequest, ctx: TenantContext
    ) -> ModelResponse:
        """Update mutable model fields. Invalidates Redis cache after write."""
        update_dict = data.model_dump(exclude_none=True)
        if "endpoint_url" in update_dict:
            update_dict["endpoint_url"] = str(update_dict["endpoint_url"])
        row = await self._repo.update(model_id=model_id, tenant_id=ctx.tenant_id, data=update_dict)
        await self._invalidate_cache(model_id)
        return ModelResponse.model_validate(row)

    async def deactivate_model(self, model_id: UUID, ctx: TenantContext) -> None:
        """Soft-delete: set is_active=False. Invalidates Redis cache."""
        await self._repo.deactivate(model_id=model_id, tenant_id=ctx.tenant_id)
        await self._invalidate_cache(model_id)

    # --- internal helpers ---

    @staticmethod
    def _build_client(record: ModelRegistry) -> AsyncOpenAI:
        """Construct an OpenAI-compatible async client. Never binds to a specific provider."""
        return AsyncOpenAI(
            base_url=record.endpoint_url,
            api_key=record.params.get("api_key", "ollama"),  # placeholder for local models
            timeout=30.0,
        )

    async def _get_record_cached(
        self, model_id: UUID, tenant_id: UUID | None
    ) -> ModelRegistry:
        """Load model from Redis cache; fall back to Postgres on miss."""
        from src.core.clients import get_redis_client
        redis = get_redis_client()
        cache_key = f"model:{model_id}"
        cached = await redis.get(cache_key)
        if cached:
            # Reconstruct a lightweight namespace object from JSON for client building.
            # Full ORM object not needed after cache hit — only endpoint_url, params, type.
            data = json.loads(cached)
            row = ModelRegistry(**data)
            # Tenant access guard still applied after cache hit
            if tenant_id is not None and row.tenant_id is not None:
                if row.tenant_id != tenant_id:
                    from src.core.exceptions import TenantIsolationError
                    raise TenantIsolationError("Model belongs to a different tenant")
            return row
        row = await self._repo.get_by_id(model_id=model_id, tenant_id=tenant_id)
        serialised = json.dumps({
            "id": str(row.id),
            "tenant_id": str(row.tenant_id) if row.tenant_id else None,
            "name": row.name,
            "type": row.type,
            "provider": row.provider,
            "endpoint_url": row.endpoint_url,
            "model_id": row.model_id,
            "params": row.params,
            "is_active": row.is_active,
        })
        await redis.set(cache_key, serialised, ex=_CACHE_TTL)
        return row

    async def _invalidate_cache(self, model_id: UUID) -> None:
        from src.core.clients import get_redis_client
        redis = get_redis_client()
        await redis.delete(f"model:{model_id}")
```

### Caching contract

- Cache key: `model:{uuid}` — stores serialised model record as JSON.
- TTL: 300 seconds (configurable via `settings.MODEL_REGISTRY_CACHE_TTL_SECONDS`; default 300).
- Invalidation: called by `update_model()` and `deactivate_model()`. Registration does not need
  invalidation (new key).
- Cache hit still applies the tenant-access guard — a cached record for tenant B must not be
  returned to tenant A's request.
- Never cache `api_key` values from `params` in plain text if the Redis instance is shared across
  tenants. Encrypt with `settings.SECRET_KEY` before writing if `api_key` is present in params.

### Seeding (seed_models.json)

```json
[
  {
    "name": "bge-m3",
    "type": "embedding",
    "provider": "ollama",
    "endpoint_url": "${EMBEDDING_BASE_URL}",
    "model_id": "BAAI/bge-m3",
    "params": {"vector_size": 1024, "max_tokens": 8192, "model_name": "BAAI/bge-m3"}
  },
  {
    "name": "bielik-11b",
    "type": "llm",
    "provider": "ollama",
    "endpoint_url": "${LLM_BASE_URL}",
    "model_id": "bielik-11b-v2.3-instruct",
    "params": {
      "model_name": "bielik-11b-v2.3-instruct",
      "context_window": 8192,
      "max_tokens": 2048,
      "temperature": 0.1
    }
  }
]
```

The seed function (called from the FastAPI lifespan in `src/main.py`) resolves `${ENV_VAR}`
placeholders from `settings` before inserting. It uses `INSERT ... ON CONFLICT DO NOTHING`
(keyed on `(name, tenant_id IS NULL)`) so that running seed on an already-populated database is
idempotent.

```python
# src/db/seed.py
import json
import os
import re
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models.model_registry import ModelRegistry


async def seed_default_models(session: AsyncSession) -> None:
    """Insert default models from seed_models.json if they do not already exist.

    Resolves ${ENV_VAR} placeholders from environment variables.
    Idempotent: skips rows whose (name, tenant_id=NULL) already exists.
    """
    seed_path = Path(__file__).parent.parent.parent / "seed_models.json"
    if not seed_path.exists():
        return
    raw = seed_path.read_text()
    # Resolve ${VAR} placeholders
    resolved = re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        raw,
    )
    entries: list[dict] = json.loads(resolved)
    from sqlalchemy import select, and_
    for entry in entries:
        stmt = select(ModelRegistry).where(
            and_(ModelRegistry.name == entry["name"], ModelRegistry.tenant_id == None)  # noqa: E711
        )
        exists = (await session.execute(stmt)).scalar_one_or_none()
        if exists is None:
            session.add(ModelRegistry(**entry))
    await session.commit()
```

---

## Implementation Steps

1. Verify TASK-002 created `src/db/models/model_registry.py` with the ORM model. If missing,
   create it now (no new Alembic migration needed — table already exists).

2. Create `src/db/repositories/model_repository.py` with `ModelRepository` (5 methods: `get_by_id`,
   `list_for_tenant`, `create`, `update`, `deactivate`).

3. Create `src/api/schemas/model_registry.py` with `ModelCreateRequest`, `ModelUpdateRequest`,
   `ModelResponse`, `ModelListResponse`, `ModelParamsEmbedding`, `ModelParamsLLM`,
   `ModelReachableResponse`.

4. Create `src/domain/model_registry.py` with `ModelRegistryService` (5 public methods plus 2
   private helpers).

5. Create `src/api/routers/models.py` with 6 endpoints. Register router in `src/main.py`:
   `app.include_router(models.router)`.

6. Add permissions `models:read` and `models:write` to the permissions seed (TASK-003). Assign
   `models:read` to all roles; `models:write` to Admin and Owner only.

7. Create `seed_models.json` in the repository root. Create `src/db/seed.py` with
   `seed_default_models()`. Call from lifespan startup in `src/main.py` after the DB pool warms up.

8. Update `src/graphs/ingest_graph/nodes/node_embed.py` (TASK-008): replace direct use of
   `settings.EMBEDDING_BASE_URL` with `await svc.get_embedding_client(collection.embedding_model_id, ctx)`.

9. Update `src/graphs/query_graph/nodes/node_generate.py` (TASK-010): replace direct use of
   `settings.LLM_BASE_URL` with `await svc.get_llm_client(pipeline.llm_model_id, ctx)`.

10. Run validation: `ruff check --fix . && ruff format . && mypy src/ && pytest -x -q`.

---

## Security Checklist

- [ ] `tenant_id` is derived exclusively from the verified JWT (`TenantContext`), never from the
  request body. The `ModelCreateRequest` schema has no `tenant_id` field.
- [ ] Tenant access guard is enforced both in `ModelRepository.get_by_id()` and in
  `_get_record_cached()` (cache hit path). A cache hit bypassing the guard is a security defect.
- [ ] `TenantIsolationError` is always mapped to HTTP 403, never 404 — to avoid leaking whether a
  model exists in another tenant's namespace.
- [ ] `api_key` values from model `params` must not appear in structlog output. Log only `model_id`
  (UUID), `tenant_id`, and model `name`.
- [ ] `validate_model_reachable()` must not follow redirects (use `httpx` with
  `follow_redirects=False`) — prevents SSRF via redirect chains to internal services.
- [ ] `endpoint_url` is validated as a valid HTTP(S) URL by Pydantic `HttpUrl`. On write, reject
  URLs pointing to `localhost`, `127.x`, or RFC-1918 ranges unless `ENVIRONMENT=development`.
  This prevents tenant Admins from registering SSRF payloads as model endpoints.
- [ ] Write endpoints (`POST`, `PATCH`, `DELETE`) write an `audit_log` entry with `actor_id`,
  `action`, `resource_type=model`, `resource_id`, `tenant_id`.
- [ ] `allowed_roles` field: if non-empty, the calling user's role IDs must intersect with the list
  before returning a client. Enforced in `get_llm_client()` and `get_embedding_client()`.
- [ ] Cache TTL must not exceed 300 seconds. Cache is stored by `model_id` only — no cross-tenant
  key construction errors possible because the tenant guard is re-applied on cache hit.

---

## Terms of Use (relevant constraints)

- All model endpoints are self-hosted on-premises. No call ever reaches an external internet
  address unless the Admin explicitly registers an `endpoint_url` pointing outside the VPN (which
  is blocked by the SSRF guard above).
- BGE-M3 (type=embedding, ADR-6) is the default embedding model. Its `model_id` in `params` must
  match the identifier understood by the Ollama/vLLM server. Mismatch causes silent wrong-dimension
  vectors — validate vector size after the first embedding call during seed.
- Changing `embedding_model_id` on a `collections` row requires a full re-indexation of that
  collection (skill `/reindex-collection`). The `ModelRegistryService` does not trigger re-indexing
  itself — it raises a `ConflictError` if any collection references the model being deactivated.
- The `/model-registry-update` skill in `.claude/skills/` calls the Admin API endpoints defined
  here to register new models. The skill is the operator interface; the API is the implementation.
- `openai` Python package is used as a pure HTTP client abstraction. It does not send telemetry
  when pointed at a local Ollama/vLLM endpoint (no OpenAI API key in the request headers for
  local providers).

---

## Tests

### Unit tests (tests/unit/domain/test_model_registry_service.py)

Use `AsyncMock` for `ModelRepository` and `redis`. No real Postgres or Redis in unit tests.

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from src.domain.model_registry import ModelRegistryService
from src.core.exceptions import NotFoundError, TenantIsolationError
from src.retrieval.schemas import TenantContext


@pytest.fixture
def ctx() -> TenantContext:
    return TenantContext(tenant_id=uuid4(), allowed_collection_ids=[])


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock()


async def test_get_llm_client_returns_openai_compatible_client(ctx, mock_session):
    """_build_client sets base_url from model record endpoint_url."""
    svc = ModelRegistryService(session=mock_session)
    record = MagicMock(
        id=uuid4(), type="llm", endpoint_url="http://gpu-host:11434/v1",
        params={}, tenant_id=None, is_active=True,
    )
    with patch.object(svc, "_get_record_cached", return_value=record):
        client = await svc.get_llm_client(model_id=record.id, ctx=ctx)
    assert str(client.base_url).startswith("http://gpu-host:11434/v1")


async def test_get_model_raises_not_found_for_inactive_model(ctx, mock_session):
    """NotFoundError is raised when the repository returns an inactive record."""
    svc = ModelRegistryService(session=mock_session)
    with patch.object(
        svc._repo, "get_by_id", side_effect=NotFoundError("inactive")
    ):
        with pytest.raises(NotFoundError):
            await svc.get_model(model_id=uuid4(), ctx=ctx)


async def test_get_model_raises_access_error_for_wrong_tenant(mock_session):
    """TenantIsolationError is raised when model.tenant_id != ctx.tenant_id."""
    tenant_a = uuid4()
    tenant_b = uuid4()
    ctx = TenantContext(tenant_id=tenant_a, allowed_collection_ids=[])
    svc = ModelRegistryService(session=mock_session)
    with patch.object(
        svc._repo, "get_by_id", side_effect=TenantIsolationError("wrong tenant")
    ):
        with pytest.raises(TenantIsolationError):
            await svc.get_model(model_id=uuid4(), ctx=ctx)


async def test_list_models_returns_system_wide_and_tenant_models(ctx, mock_session):
    """list_models includes models with tenant_id=NULL and with ctx.tenant_id."""
    global_model = MagicMock(tenant_id=None, name="bge-m3", type="embedding")
    tenant_model = MagicMock(tenant_id=ctx.tenant_id, name="custom-llm", type="llm")
    svc = ModelRegistryService(session=mock_session)
    with patch.object(svc._repo, "list_for_tenant", return_value=[global_model, tenant_model]):
        results = await svc.list_models(ctx=ctx)
    assert len(results) == 2


async def test_validate_model_reachable_returns_false_on_timeout(ctx, mock_session):
    """validate_model_reachable returns False on httpx.TimeoutException without raising."""
    import httpx
    svc = ModelRegistryService(session=mock_session)
    record = MagicMock(
        id=uuid4(), type="llm", endpoint_url="http://unreachable:11434/v1",
        params={}, tenant_id=None, is_active=True,
    )
    with patch.object(svc, "_get_record_cached", return_value=record):
        with patch("httpx.AsyncClient.get", side_effect=httpx.TimeoutException("timeout")):
            result = await svc.validate_model_reachable(model_id=record.id, ctx=ctx)
    assert result is False


async def test_model_client_cache_hit_avoids_db_query(ctx, mock_session):
    """A Redis cache hit must not issue a Postgres query."""
    svc = ModelRegistryService(session=mock_session)
    model_id = uuid4()
    cached_data = {
        "id": str(model_id), "tenant_id": None, "name": "bge-m3",
        "type": "embedding", "provider": "ollama",
        "endpoint_url": "http://gpu:11434/v1", "model_id": "BAAI/bge-m3",
        "params": {"vector_size": 1024}, "is_active": True,
    }
    import json
    mock_redis = AsyncMock()
    mock_redis.get.return_value = json.dumps(cached_data)
    with patch("src.core.clients.get_redis_client", return_value=mock_redis):
        await svc._get_record_cached(model_id, ctx.tenant_id)
    svc._repo.get_by_id.assert_not_awaited()


async def test_register_model_requires_admin_role(async_client, admin_token, viewer_token):
    """POST /models returns 403 for a Viewer token and 201 for an Admin token."""
    payload = {
        "name": "test-llm", "type": "llm", "provider": "ollama",
        "endpoint_url": "http://gpu:11434/v1", "model_id": "test-model", "params": {},
    }
    resp_viewer = await async_client.post(
        "/models", json=payload, headers={"Authorization": f"Bearer {viewer_token}"}
    )
    assert resp_viewer.status_code == 403

    resp_admin = await async_client.post(
        "/models", json=payload, headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert resp_admin.status_code == 201
```

### Integration tests (tests/integration/test_model_registry.py)

Use testcontainers for Postgres and Redis.

```python
@pytest.mark.integration
async def test_ingest_graph_uses_embedding_model_from_registry(
    pg_session, redis_client, registered_embedding_model
):
    """node_embed calls get_embedding_client() and receives a client with the correct base_url."""
    from src.graphs.ingest_graph.nodes.node_embed import node_embed
    from unittest.mock import patch, AsyncMock

    ctx = TenantContext(
        tenant_id=registered_embedding_model.tenant_id or uuid4(),
        allowed_collection_ids=[],
    )
    with patch("src.domain.model_registry.ModelRegistryService.get_embedding_client") as mock_get:
        mock_get.return_value = AsyncMock()
        await node_embed(state=..., ctx=ctx, session=pg_session)
    mock_get.assert_awaited_once()
    call_args = mock_get.call_args
    assert call_args.kwargs["model_id"] == registered_embedding_model.id


@pytest.mark.integration
async def test_query_graph_uses_llm_from_registry(pg_session, registered_llm_model):
    """node_generate calls get_llm_client() and receives a client with the correct base_url."""
    from src.graphs.query_graph.nodes.node_generate import node_generate
    from unittest.mock import patch, AsyncMock

    ctx = TenantContext(tenant_id=uuid4(), allowed_collection_ids=[uuid4()])
    with patch("src.domain.model_registry.ModelRegistryService.get_llm_client") as mock_get:
        mock_get.return_value = AsyncMock()
        await node_generate(state=..., ctx=ctx, session=pg_session)
    mock_get.assert_awaited_once()
```

### HTTP endpoint tests (tests/unit/api/test_models_router.py)

| Test | Setup | Assert |
|------|-------|--------|
| `test_list_models_returns_200` | Viewer JWT | 200 + `items` list |
| `test_list_models_401_no_token` | No auth header | 401 |
| `test_get_model_404_inactive` | Inactive model | 404 |
| `test_get_model_403_wrong_tenant` | Wrong tenant JWT | 403 |
| `test_create_model_201_admin` | Admin JWT, valid payload | 201 + model in response |
| `test_create_model_403_viewer` | Viewer JWT | 403 |
| `test_create_model_422_missing_fields` | Payload missing `type` | 422 |
| `test_patch_model_200_admin` | Admin JWT, `{"params": {...}}` | 200 + updated response |
| `test_patch_model_403_viewer` | Viewer JWT | 403 |
| `test_delete_model_204_admin` | Admin JWT | 204 |
| `test_delete_model_403_viewer` | Viewer JWT | 403 |
| `test_check_model_reachable_200` | Mocked httpx 200 | `{"reachable": true}` |
| `test_check_model_reachable_false_on_timeout` | Mocked httpx timeout | `{"reachable": false}` |

---

## Definition of Done

- [ ] `src/domain/model_registry.py` — `ModelRegistryService` with 5 public methods:
  `get_model`, `get_llm_client`, `get_embedding_client`, `list_models`, `validate_model_reachable`,
  plus `register_model`, `update_model`, `deactivate_model`.
- [ ] `src/db/repositories/model_repository.py` — `ModelRepository` with `get_by_id`,
  `list_for_tenant`, `create`, `update`, `deactivate`.
- [ ] `src/api/schemas/model_registry.py` — all Pydantic schemas with type annotations.
- [ ] `src/api/routers/models.py` — all 6 endpoints; router registered in `src/main.py`.
- [ ] `seed_models.json` committed; `src/db/seed.py` `seed_default_models()` callable from lifespan.
- [ ] Redis caching with TTL 300s; cache invalidated on `PATCH` and `DELETE`.
- [ ] Tenant access guard applied on both cache hit and cache miss paths.
- [ ] Permissions `models:read` and `models:write` seeded; role assignments documented.
- [ ] Audit log entries written for `POST`, `PATCH`, `DELETE /models`.
- [ ] SSRF guard on `endpoint_url` in non-development environments.
- [ ] All unit tests pass: `pytest tests/unit/domain/test_model_registry_service.py -x -q`.
- [ ] All HTTP endpoint tests pass: `pytest tests/unit/api/test_models_router.py -x -q`.
- [ ] Integration tests pass: `pytest tests/integration/test_model_registry.py -x -q`.
- [ ] `mypy src/domain/model_registry.py src/db/repositories/model_repository.py src/api/routers/models.py` exits zero.
- [ ] `ruff check --fix .` exits zero.
- [ ] TASK-008 (`node_embed`) updated to call `ModelRegistryService.get_embedding_client()`.
- [ ] TASK-010 (`node_generate`) updated to call `ModelRegistryService.get_llm_client()`.
- [ ] `docs/03-Specyfikacja-API.md` (api.md) updated with the 6 new endpoints.
