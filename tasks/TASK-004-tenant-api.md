# TASK-004: Tenant Management API

**Status:** TODO
**Priority:** P1 — required before user onboarding
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile), security-auditor
**Related docs:** `docs/data-model.md` §2.1 | `docs/architecture.md` §13
**Estimated effort:** 3–4 days

---

## Overview

Implement the full tenant management API: CRUD for tenants (owner/platform-admin only), user membership management (`user_tenants`), and role assignment (`user_roles`). This covers provisioning new organizations onto the platform, managing who belongs to each tenant, and controlling what roles users hold within their tenant.

All responses are filtered by the `ctx.tenant_id` from JWT. The system roles (`admin`, `contributor`, `viewer`) are seeded automatically when a tenant is created. Every write operation inserts an entry into `audit_log`.

## Usage

Tenant creation is a privileged operation performed by a platform operator (user with the global `admin` role). Tenant member management is performed by a tenant admin (user with the `admin` role within that tenant). Role assignments are also admin-only within each tenant.

**Who calls these endpoints:**
- Platform operator scripts (tenant onboarding): `POST /tenants`
- Tenant admins (user management): `POST /tenants/{id}/users`, `DELETE /tenants/{id}/users/{user_id}`, `PUT /tenants/{id}/users/{user_id}/role`
- Open WebUI (indirectly, to show tenant info): `GET /tenants/{id}`

## Tech Stack

- **FastAPI** — router with `APIRouter(prefix="/tenants")`
- **SQLAlchemy 2.x `AsyncSession`** — all DB operations async
- **Pydantic v2** — request/response schemas with validators
- **structlog** — audit events logged before DB write (for distributed tracing correlation)
- TASK-003 dependencies: `get_current_ctx`, `require_permission`, `assert_tenant_owns_resource`
- TASK-002 models: `Tenant`, `User`, `UserTenant`, `UserRole`, `Role`

## Database Patterns

### Tables Used

- `tenants` — CRUD
- `users` — lookup by `keycloak_sub` or `email` when adding to tenant
- `user_tenants` — membership join table
- `roles` — seeded system roles; per-tenant role lookup
- `user_roles` — role assignment within tenant
- `audit_log` — append-only audit entries

### Tenant Repository

```python
# src/db/repositories/tenant_repository.py
import uuid
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Tenant, User, UserTenant, Role, UserRole, AuditLog
from src.core.exceptions import ConflictError, NotFoundError


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        q = select(Tenant).where(Tenant.slug == slug)
        return (await self._session.execute(q)).scalar_one_or_none()

    async def create(
        self,
        name: str,
        slug: str,
        settings: dict,
    ) -> Tenant:
        existing = await self.get_by_slug(slug)
        if existing:
            raise ConflictError(f"Tenant with slug '{slug}' already exists")

        tenant = Tenant(name=name, slug=slug, settings=settings, status="active")
        self._session.add(tenant)
        await self._session.flush()  # Get the generated ID

        # Seed system roles for this tenant
        for role_name in ("admin", "contributor", "viewer"):
            role = Role(tenant_id=tenant.id, name=role_name, is_system=True)
            self._session.add(role)
        await self._session.flush()
        return tenant

    async def update_settings(
        self, tenant: Tenant, settings: dict
    ) -> Tenant:
        tenant.settings = settings  # type: ignore[assignment]
        await self._session.flush()
        return tenant

    async def soft_delete(self, tenant: Tenant) -> None:
        tenant.status = "deleted"
        await self._session.flush()

    async def add_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> UserTenant:
        # Check not already a member
        q = select(UserTenant).where(
            UserTenant.tenant_id == tenant_id,
            UserTenant.user_id == user_id,
        )
        existing = (await self._session.execute(q)).scalar_one_or_none()
        if existing:
            raise ConflictError("User is already a member of this tenant")
        membership = UserTenant(user_id=user_id, tenant_id=tenant_id)
        self._session.add(membership)
        await self._session.flush()
        return membership

    async def remove_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        q = select(UserTenant).where(
            UserTenant.tenant_id == tenant_id,
            UserTenant.user_id == user_id,
        )
        membership = (await self._session.execute(q)).scalar_one_or_none()
        if membership is None:
            raise NotFoundError("User is not a member of this tenant")
        await self._session.delete(membership)
        # Cascade: also delete user_roles for this user within this tenant
        role_ids_q = select(Role.id).where(Role.tenant_id == tenant_id)
        from sqlalchemy import delete as sa_delete
        await self._session.execute(
            sa_delete(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id.in_(role_ids_q),
            )
        )
        await self._session.flush()

    async def assign_role(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role_name: str,
    ) -> None:
        # Resolve role within tenant
        role_q = select(Role).where(
            Role.tenant_id == tenant_id, Role.name == role_name
        )
        role = (await self._session.execute(role_q)).scalar_one_or_none()
        if role is None:
            raise NotFoundError(f"Role '{role_name}' not found in tenant")

        # Remove existing roles in this tenant first (one active role policy)
        existing_role_ids_q = select(Role.id).where(Role.tenant_id == tenant_id)
        from sqlalchemy import delete as sa_delete
        await self._session.execute(
            sa_delete(UserRole).where(
                UserRole.user_id == user_id,
                UserRole.role_id.in_(existing_role_ids_q),
            )
        )
        # Assign new role
        self._session.add(UserRole(user_id=user_id, role_id=role.id))
        await self._session.flush()
```

### Audit Log Helper

```python
# src/domain/audit_service.py
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import AuditLog
from src.api.schemas.auth import UserContext


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        ctx: UserContext,
        action: str,
        resource_type: str,
        resource_id: uuid.UUID,
        details: dict | None = None,
        ip: str | None = None,
    ) -> None:
        entry = AuditLog(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip=ip,
        )
        self._session.add(entry)
        # Do NOT flush here — caller manages transaction boundary
```

## API Contracts

### Schemas

```python
# src/api/schemas/tenant.py
from __future__ import annotations
import re
import uuid
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class TenantSettings(BaseModel):
    """JSONB column schema for tenants.settings."""
    max_docs: int = Field(default=10_000, ge=1)
    max_collections: int = Field(default=50, ge=1)
    max_users: int = Field(default=100, ge=1)
    max_storage_bytes: int = Field(default=50 * 1024**3)  # 50 GB
    retention_days: int = Field(default=365, ge=30)
    industry: str = Field(default="general", max_length=50)
    disclaimer_text: str = Field(default="", max_length=2000)


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    settings: TenantSettings = Field(default_factory=TenantSettings)

    @field_validator("slug")
    @classmethod
    def slug_no_leading_trailing_dash(cls, v: str) -> str:
        if v.startswith("-") or v.endswith("-"):
            raise ValueError("Slug must not start or end with a dash")
        return v


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings: TenantSettings | None = None


class TenantResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    settings: TenantSettings
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AddUserRequest(BaseModel):
    """Request body for POST /tenants/{id}/users"""
    keycloak_sub: str = Field(..., min_length=1, max_length=255)


class AssignRoleRequest(BaseModel):
    """Request body for PUT /tenants/{id}/users/{user_id}/role"""
    role: str = Field(..., pattern=r"^(admin|contributor|viewer)$")


class TenantMemberResponse(BaseModel):
    user_id: uuid.UUID
    keycloak_sub: str
    email: str
    display_name: str | None
    role: str | None
    joined_at: datetime

    model_config = {"from_attributes": True}
```

### Endpoints

```python
# src/api/routers/tenants.py
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import (
    assert_tenant_owns_resource, get_current_ctx, require_permission
)
from src.api.schemas.auth import UserContext
from src.api.schemas.tenant import (
    AddUserRequest, AssignRoleRequest, TenantCreate, TenantMemberResponse,
    TenantResponse, TenantUpdate
)
from src.core.database import get_db_session
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.domain.tenant_service import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])
```

#### `POST /tenants`

- **Permission:** `admin:tenants` (platform-level admin; not a regular tenant role)
- **Body:** `TenantCreate`
- **Response 201:** `TenantResponse`
- **409:** slug already taken
- **Audit log action:** `tenant.created`

```python
@router.post("", status_code=status.HTTP_201_CREATED, response_model=TenantResponse)
async def create_tenant(
    body: TenantCreate,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(require_permission("admin:tenants"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
) -> TenantResponse:
    repo = TenantRepository(session)
    tenant = await repo.create(
        name=body.name, slug=body.slug, settings=body.settings.model_dump()
    )
    await AuditService(session).log(
        ctx=ctx,
        action="tenant.created",
        resource_type="tenant",
        resource_id=tenant.id,
        details={"slug": tenant.slug},
        ip=request.client.host if request.client else None,
    )
    await session.commit()
    return TenantResponse.model_validate(tenant)
```

#### `GET /tenants/{tenant_id}`

- **Permission:** any authenticated user (filtered to their own tenant)
- **Response 200:** `TenantResponse`
- **403:** tenant_id does not match `ctx.tenant_id`

#### `PATCH /tenants/{tenant_id}`

- **Permission:** `admin:tenants` (within the tenant) or platform admin
- **Body:** `TenantUpdate` (partial update)
- **Response 200:** `TenantResponse`
- **Audit log action:** `tenant.updated`

#### `DELETE /tenants/{tenant_id}`

- **Permission:** `admin:tenants` (platform admin only)
- **Response 204:** No content
- **Audit log action:** `tenant.deleted`
- **Note:** Soft-delete only (`status = 'deleted'`). Does NOT cascade delete documents, collections, or users — that is a separate offboarding procedure.

#### `GET /tenants/{tenant_id}/users`

- **Permission:** `admin:users`
- **Response 200:** `list[TenantMemberResponse]`
- **Query params:** `page`, `page_size`

#### `POST /tenants/{tenant_id}/users`

- **Permission:** `admin:users`
- **Body:** `AddUserRequest` (keycloak_sub of the user to add)
- **Response 201:** `TenantMemberResponse`
- **404:** user with given `keycloak_sub` not found in `users` table
- **409:** user already a member
- **Audit log action:** `admin.user_added`

#### `DELETE /tenants/{tenant_id}/users/{user_id}`

- **Permission:** `admin:users`
- **Response 204:** No content
- **Audit log action:** `admin.user_removed`
- **Note:** Also removes all `user_roles` for this user within this tenant

#### `PUT /tenants/{tenant_id}/users/{user_id}/role`

- **Permission:** `admin:users`
- **Body:** `AssignRoleRequest`
- **Response 200:** `TenantMemberResponse`
- **Audit log action:** `admin.role_change`
- **Side effect:** calls `invalidate_user_context_cache(keycloak_sub, tenant_id)` to immediately invalidate the 60s auth cache for this user

## Architecture — SOLID & DRY

### Service Layer

```python
# src/domain/tenant_service.py
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.auth import UserContext
from src.api.schemas.tenant import TenantCreate, TenantUpdate, TenantResponse
from src.core.exceptions import NotFoundError, TenantIsolationError
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.core.clients.minio_client import create_tenant_bucket


class TenantService:
    """
    Encapsulates all business logic for tenant lifecycle.
    Routers call this; they do not call repos directly.
    """
    def __init__(self, session: AsyncSession) -> None:
        self._repo = TenantRepository(session)
        self._audit = AuditService(session)
        self._session = session

    async def create_tenant(
        self, body: TenantCreate, ctx: UserContext, ip: str | None
    ) -> TenantResponse:
        tenant = await self._repo.create(
            name=body.name,
            slug=body.slug,
            settings=body.settings.model_dump(),
        )
        # Provision MinIO bucket: tenant-{slug}
        await create_tenant_bucket(tenant.slug)

        await self._audit.log(
            ctx=ctx, action="tenant.created",
            resource_type="tenant", resource_id=tenant.id,
            details={"slug": tenant.slug}, ip=ip,
        )
        return TenantResponse.model_validate(tenant)
```

**What NOT to do:**
- Routers MUST NOT instantiate `TenantRepository` directly
- Routers MUST NOT write to `audit_log` directly — always via `AuditService`
- Services MUST NOT call `session.commit()` — the router commits after the service returns
- Never return SQLAlchemy ORM model instances from services — always convert to Pydantic schema

### Tenant Isolation Enforcement

Every route handler that takes `{tenant_id}` as a path parameter must call `assert_tenant_owns_resource()` before returning:

```python
# Pattern used in EVERY handler that loads a resource by ID
async def get_tenant_handler(
    tenant_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantResponse:
    assert_tenant_owns_resource(tenant_id, ctx)  # First thing — before DB query
    tenant = await TenantRepository(session).get_by_id(tenant_id)
    if tenant is None:
        # After isolation check — 404 only revealed to members of this tenant
        raise NotFoundError("Tenant not found")
    return TenantResponse.model_validate(tenant)
```

## Implementation Steps

1. **Create `src/api/schemas/tenant.py`** — all schemas as shown

2. **Create `src/domain/audit_service.py`** — `AuditService.log()` as shown

3. **Create `src/db/repositories/tenant_repository.py`** — full repository

4. **Create `src/domain/tenant_service.py`** — service layer (thin wrapper over repo + audit + MinIO bucket creation)

5. **Create MinIO client helper** `src/core/clients/minio_client.py`:
   ```python
   from minio import Minio
   from src.core.config import settings

   def get_minio_client() -> Minio:
       return Minio(
           settings.MINIO_ENDPOINT,
           access_key=settings.MINIO_ACCESS_KEY,
           secret_key=settings.MINIO_SECRET_KEY,
           secure=settings.MINIO_USE_TLS,
       )

   async def create_tenant_bucket(slug: str) -> None:
       """Create tenant-{slug} bucket with SSE and versioning enabled."""
       client = get_minio_client()
       bucket_name = f"tenant-{slug}"
       # Run in thread pool — minio-py is synchronous
       import asyncio
       loop = asyncio.get_event_loop()
       await loop.run_in_executor(None, _create_bucket_sync, client, bucket_name)

   def _create_bucket_sync(client: Minio, bucket_name: str) -> None:
       if not client.bucket_exists(bucket_name):
           client.make_bucket(bucket_name)
           # Enable versioning
           from minio.versioningconfig import VersioningConfig, ENABLED
           client.set_bucket_versioning(bucket_name, VersioningConfig(ENABLED))
   ```

6. **Create `src/api/routers/tenants.py`** — all 8 endpoint handlers

7. **Register router in `src/main.py`:**
   ```python
   from src.api.routers import tenants
   app.include_router(tenants.router, prefix="/api/v1")
   ```

8. **Write tests** (see Tests section)

9. **Run:** `ruff check --fix . && mypy src/ && pytest -x -q tests/unit/test_tenant_api.py tests/security/test_tenant_isolation.py`

## Security Checklist

- `assert_tenant_owns_resource(tenant_id, ctx)` called as the first operation in every route handler that takes a path-level `tenant_id`
- Admin user cannot manage another tenant's users (tenant_id from path vs ctx.tenant_id)
- `DELETE /tenants/{id}/users/{user_id}` also deletes `user_roles` for that user in this tenant — no orphan permissions
- Role assignment calls `invalidate_user_context_cache()` after DB commit — immediate effect
- `audit_log` entries for all write operations: create, update, delete tenant; add/remove user; change role
- `audit_log.details` MUST NOT contain: email, display_name, PII; only resource IDs and action metadata
- MinIO bucket creation is idempotent (check `bucket_exists` first)
- Slug validation: `^[a-z0-9-]+$` prevents path traversal in MinIO bucket names
- Tenant soft-delete does not cascade to MinIO bucket or Qdrant — this is intentional (data preserved for audit)

## Terms of Use (relevant constraints)

- GDPR Art. 5: purpose limitation — tenant settings must not be used to bypass per-tenant data retention limits configured by the data controller (tenant admin)
- Medical context: tenant status changes (`suspended`, `deleted`) must be audited; clinic data must remain accessible to auditors even after tenant suspension
- `max_storage_bytes` in tenant settings is informational in MVP (not enforced by API); enforcement added in Phase 2

## Tests

References TASK-016 `tests/integration/test_api_contracts.py`.

**`tests/unit/test_tenant_api.py`**

- `POST /tenants` with valid body → 201 with correct `TenantResponse`
- `POST /tenants` with duplicate slug → 409
- `POST /tenants` as Viewer (no `admin:tenants` permission) → 403
- `POST /tenants` without auth → 401
- `GET /tenants/{id}` for user's own tenant → 200
- `GET /tenants/{id}` with different tenant_id than `ctx.tenant_id` → 403
- `PATCH /tenants/{id}` partial update (only `name`) → 200, only name changed
- `PATCH /tenants/{id}` with invalid settings (`retention_days < 30`) → 422
- `DELETE /tenants/{id}` → 204, tenant status is `deleted`
- `POST /tenants/{id}/users` with valid `keycloak_sub` → 201
- `POST /tenants/{id}/users` with unknown `keycloak_sub` → 404
- `POST /tenants/{id}/users` adding already-member → 409
- `DELETE /tenants/{id}/users/{user_id}` → 204, also removes user_roles
- `PUT /tenants/{id}/users/{user_id}/role` to `contributor` → 200
- `PUT /tenants/{id}/users/{user_id}/role` with invalid role (`superadmin`) → 422

**`tests/security/test_tenant_isolation.py`** (mark: `@pytest.mark.tenant_isolation`)

- User from tenant A calls `GET /tenants/{tenant_B_id}` → 403
- User from tenant A calls `DELETE /tenants/{tenant_A_id}/users/{user_from_tenant_B}` → 404
- Audit log is written for all successful write operations
- Role change via `PUT .../role` invalidates the auth cache for the affected user

**Authorization matrix tests (per role):**

| Endpoint | Owner/PlatformAdmin | Admin | Contributor | Viewer |
|---|---|---|---|---|
| `POST /tenants` | 201 | 403 | 403 | 403 |
| `GET /tenants/{id}` | 200 | 200 | 200 | 200 |
| `PATCH /tenants/{id}` | 200 | 403 | 403 | 403 |
| `DELETE /tenants/{id}` | 204 | 403 | 403 | 403 |
| `POST /tenants/{id}/users` | 201 | 201 | 403 | 403 |
| `DELETE /tenants/{id}/users/{uid}` | 204 | 204 | 403 | 403 |
| `PUT /tenants/{id}/users/{uid}/role` | 200 | 200 | 403 | 403 |

## Definition of Done

- [ ] All 8 tenant endpoints implemented with correct HTTP status codes
- [ ] `TenantSettings` Pydantic model with validation (min/max bounds)
- [ ] `TenantRepository` with create (incl. role seeding), update, soft-delete, user management
- [ ] `TenantService` as intermediary (routers → service → repo)
- [ ] `AuditService.log()` called for every write operation
- [ ] MinIO bucket created (idempotently) on tenant creation
- [ ] `invalidate_user_context_cache()` called after role changes
- [ ] `assert_tenant_owns_resource()` called first in every handler with tenant path param
- [ ] All unit tests for tenant API pass
- [ ] `@pytest.mark.tenant_isolation` tests pass
- [ ] Authorization matrix tests pass (all 4 roles, all 8 endpoints)
- [ ] `ruff check . && mypy src/` exit zero
