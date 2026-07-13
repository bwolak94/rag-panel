# TASK-003: Authentication and RBAC

**Status:** TODO
**Priority:** P0 — required before any endpoint implementation
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile), security-auditor
**Related docs:** `docs/architecture.md` §10, §13 | `docs/data-model.md` §2.1
**Estimated effort:** 3–4 days

---

## Overview

Implement the complete authentication and authorization layer for the RAG platform. This includes Keycloak JWT verification using RS256 signature and JWKS endpoint, the `UserContext` dataclass, the `get_current_ctx` FastAPI dependency, role-based and permission-based access control dependencies (`require_permission()`, `require_role_min()`), per-collection access level enforcement, and tenant context injection middleware.

This task is a security blocker. The `/tenant-isolation-check` skill must pass before this task is marked done. All tests in `tests/security/` referencing JWT and RBAC must be green.

## Usage

Every protected endpoint declares the `get_current_ctx` dependency in its signature plus one or more permission guards:

```python
# Example: how other tasks use the dependencies from this task
from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.auth import UserContext

@router.post("/documents")
async def upload_document(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(require_permission("documents:upload"))],
    body: DocumentUploadRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentResponse:
    ...
```

The `UserContext` carries everything downstream services need: `user_id`, `tenant_id`, `roles`, `permissions`, `allowed_collection_ids`. No handler reads `tenant_id` from the request body or query string.

## Tech Stack

- **python-jose[cryptography] 3.3+** — JWT decode and RS256 signature verification with JWKS; `cryptography` extra required for RSA key handling
- **httpx 0.27+** — async JWKS fetch with caching; never blocking
- **cachetools** — `TTLCache` for JWKS key cache (5-minute TTL, served stale on Keycloak failure per `docs/architecture.md §16`)
- **FastAPI** — `Depends()` dependency injection, `HTTPBearer` security scheme
- **SQLAlchemy 2.x `AsyncSession`** — `allowed_collection_ids` lookup (cached 60 seconds per user)

## Database Patterns

Queries needed by the auth layer (all scoped by tenant from JWT, never from body):

```python
# Fetch user's permissions for a given tenant
SELECT p.code
FROM permissions p
JOIN role_permissions rp ON rp.permission_id = p.id
JOIN user_roles ur ON ur.role_id = rp.role_id
JOIN users u ON u.id = ur.user_id
WHERE u.keycloak_sub = :keycloak_sub
  AND ur.role_id IN (
      SELECT id FROM roles WHERE tenant_id = :tenant_id
  );

# Fetch allowed collection IDs for user's roles within tenant
SELECT ca.collection_id, ca.access_level
FROM collection_access ca
JOIN user_roles ur ON ur.role_id = ca.role_id
JOIN users u ON u.id = ur.user_id
WHERE u.keycloak_sub = :keycloak_sub
  AND ca.collection_id IN (
      SELECT id FROM collections WHERE tenant_id = :tenant_id AND is_active = true
  );
```

Both queries are cached in-process with a 60-second TTL per `(keycloak_sub, tenant_id)` key.

## API Contracts

No HTTP endpoints in this task. This task provides FastAPI dependencies consumed by all other tasks.

### `UserContext` Schema

> **Architecture note:** `UserContext` is a frozen dataclass defined in `src/domain/auth.py` and imported by both `src/db/repositories/auth_repository.py` and `src/api/schemas/auth.py` (never the reverse). The `src/api/schemas/auth.py` module re-exports it for convenience; `AuthRepository` and all domain code import exclusively from `src.domain.auth`.

```python
# src/domain/auth.py
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class UserContext:
    """
    Immutable user context extracted from validated JWT and DB lookup.
    This is the sole source of identity for all business logic.
    Never extracted from request body or query parameters.
    """
    user_id: UUID
    keycloak_sub: str
    email: str
    display_name: str
    tenant_id: UUID
    roles: frozenset[str]            # Role names within this tenant
    permissions: frozenset[str]      # Permission codes (e.g., "documents:upload")
    allowed_collection_ids: frozenset[UUID]   # Collections readable by user
    writable_collection_ids: frozenset[UUID]  # Collections writable by user

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def can_read_collection(self, collection_id: UUID) -> bool:
        return collection_id in self.allowed_collection_ids

    def can_write_collection(self, collection_id: UUID) -> bool:
        return collection_id in self.writable_collection_ids
```

### JWT Claims Contract

Keycloak JWT tokens for this platform contain the following custom claims. These are configured in Keycloak via a Client Scope mapper:

```json
{
  "sub": "kc-user-uuid",
  "iss": "https://auth.example.com/realms/rag-platform",
  "aud": "rag-api",
  "exp": 1753000000,
  "iat": 1752996400,
  "email": "user@example.com",
  "preferred_username": "user@example.com",
  "name": "Full Name",
  "tenant_id": "uuid-of-tenant",
  "realm_access": {
    "roles": ["contributor"]
  }
}
```

The `tenant_id` claim is injected by a Keycloak mapper from the user's `user_tenants` row. A user can only be logged into one tenant at a time (tenant is part of the session, not switchable via API without re-authentication).

## Architecture — SOLID & DRY

### JWKS Client

```python
# src/core/clients/jwks.py
import asyncio
from typing import Any

import httpx
from cachetools import TTLCache
from jose import jwk, jwt

from src.core.config import settings

_jwks_cache: TTLCache[str, list[dict[str, Any]]] = TTLCache(maxsize=1, ttl=300)
_jwks_lock = asyncio.Lock()
# Separate variable for stale fallback — never access _jwks_cache._data (private API).
# Updated on every successful JWKS fetch; read as fallback when Keycloak is unreachable.
_last_good_jwks: list[dict[str, Any]] | None = None


async def get_public_keys() -> list[dict[str, Any]]:
    """
    Fetch and cache JWKS from Keycloak. Returns stale cache on network failure.
    Timeout per docs/architecture.md §16: connect=3s, read=5s.
    """
    global _last_good_jwks

    cache_key = "jwks"
    if cache_key in _jwks_cache:
        return _jwks_cache[cache_key]

    async with _jwks_lock:
        # Double-checked locking pattern
        if cache_key in _jwks_cache:
            return _jwks_cache[cache_key]

        jwks_url = (
            f"{settings.KEYCLOAK_BASE_URL}/realms/{settings.KEYCLOAK_REALM}"
            "/protocol/openid-connect/certs"
        )
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=5.0)) as client:
                response = await client.get(jwks_url)
                response.raise_for_status()
                keys: list[dict[str, Any]] = response.json()["keys"]
                _jwks_cache[cache_key] = keys
                _last_good_jwks = keys  # Update stale fallback on every successful fetch
                return keys
        except Exception:
            # Serve stale cache on failure — prefer availability over security
            # (tokens already issued remain valid; we cannot revoke them anyway)
            if _last_good_jwks is not None:
                return _last_good_jwks
            raise


def verify_token(token: str, public_keys: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Verify RS256 JWT signature and standard claims.
    Raises jose.JWTError on any validation failure.
    """
    # Try each key in the JWKS (key rotation support)
    last_error: Exception | None = None
    for key_data in public_keys:
        try:
            public_key = jwk.construct(key_data)
            claims: dict[str, Any] = jwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                audience=settings.KEYCLOAK_AUDIENCE,
                options={"verify_exp": True, "verify_aud": True},
            )
            return claims
        except (jwt.JWTError, jwt.ExpiredSignatureError, jwt.InvalidTokenError) as exc:
            last_error = exc
            continue
        except Exception as exc:
            logger.error("unexpected_jwks_error", error=str(type(exc).__name__))
            raise AuthenticationError("Token verification failed") from exc
    raise last_error or RuntimeError("No JWKS keys available")
```

### Auth Dependencies

```python
# src/api/dependencies/auth.py
from collections.abc import Callable
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.domain.auth import UserContext
from src.core.clients.jwks import get_public_keys, verify_token
from src.core.exceptions import PermissionDeniedError, TenantIsolationError
from src.db.repositories.auth_repository import AuthRepository
from src.core.database import get_db_session

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=True)


async def get_current_ctx(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserContext:
    """
    Primary auth dependency. Validates JWT, resolves user+tenant from DB,
    builds immutable UserContext.

    Raises:
        HTTPException(401): token missing, expired, invalid signature
        HTTPException(403): user not a member of the tenant in token
    """
    token = credentials.credentials
    try:
        public_keys = await get_public_keys()
        claims = verify_token(token, public_keys)
    except Exception as exc:
        logger.warning("jwt_validation_failed", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Extract claims — NEVER from body/query
    keycloak_sub: str = claims["sub"]
    tenant_id_str: str | None = claims.get("tenant_id")
    email: str = claims.get("email", "")
    display_name: str = claims.get("name", "")

    if not tenant_id_str:
        raise HTTPException(status_code=401, detail="Token missing tenant_id claim")

    try:
        tenant_id = UUID(tenant_id_str)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid tenant_id in token")

    repo = AuthRepository(session)
    user_ctx = await repo.build_user_context(
        keycloak_sub=keycloak_sub,
        tenant_id=tenant_id,
        email=email,
        display_name=display_name,
    )
    if user_ctx is None:
        # User is not a member of this tenant — 403, not 404
        raise HTTPException(status_code=403, detail="Access denied")

    return user_ctx


def require_permission(permission_code: str) -> Callable[[UserContext], None]:
    """
    Returns a FastAPI dependency that raises 403 if the user lacks the permission.

    Usage:
        _ = Depends(require_permission("documents:upload"))
    """
    async def _check(ctx: Annotated[UserContext, Depends(get_current_ctx)]) -> None:
        if not ctx.has_permission(permission_code):
            logger.warning(
                "permission_denied",
                user_id=str(ctx.user_id),
                permission=permission_code,
            )
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    return _check


def require_collection_read(collection_id: UUID, ctx: UserContext) -> None:
    """
    Resource-level check: user must have read access to the given collection.
    Call inside a route handler after resolving collection_id.
    Raises TenantIsolationError if collection does not belong to ctx.tenant_id.
    """
    if not ctx.can_read_collection(collection_id):
        raise PermissionDeniedError(f"No read access to collection {collection_id}")


def require_collection_write(collection_id: UUID, ctx: UserContext) -> None:
    """Resource-level check: user must have write access."""
    if not ctx.can_write_collection(collection_id):
        raise PermissionDeniedError(f"No write access to collection {collection_id}")


def assert_tenant_owns_resource(resource_tenant_id: UUID, ctx: UserContext) -> None:
    """
    Hard tenant isolation check. Called in every handler that loads a resource by ID.
    Raises TenantIsolationError (maps to 403) if mismatch.

    This MUST be called before returning or modifying any resource.
    """
    if resource_tenant_id != ctx.tenant_id:
        logger.error(
            "tenant_isolation_violation",
            ctx_tenant=str(ctx.tenant_id),
            resource_tenant=str(resource_tenant_id),
            user_id=str(ctx.user_id),
        )
        raise TenantIsolationError("Access denied")
```

### Auth Repository

```python
# src/db/repositories/auth_repository.py
from uuid import UUID
import asyncio
from cachetools import TTLCache

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.auth import UserContext
from src.db.models import User, UserTenant, UserRole, Role, RolePermission, Permission, CollectionAccess, Collection

# Cache per (keycloak_sub, tenant_id) — 60 second TTL
# NOTE (MAJOR): Cache population must be guarded with `asyncio.Lock` to prevent
# thundering herd under concurrent requests (multiple coroutines may enter
# `build_user_context` simultaneously for the same key before the first one
# populates the cache). Use `async with _ctx_cache_lock:` pattern around the
# cache-miss branch, or replace with `aiocache` for production.
# Example: add `_ctx_cache_lock: asyncio.Lock = asyncio.Lock()` at module level
# and wrap the entire DB query block with `async with _ctx_cache_lock:` after
# checking the cache a second time inside the lock (double-checked locking).
_ctx_cache: TTLCache[tuple[str, UUID], UserContext] = TTLCache(maxsize=1000, ttl=60)
_ctx_cache_lock: asyncio.Lock = asyncio.Lock()


class AuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build_user_context(
        self,
        keycloak_sub: str,
        tenant_id: UUID,
        email: str,
        display_name: str,
    ) -> UserContext | None:
        cache_key = (keycloak_sub, tenant_id)
        if cache_key in _ctx_cache:
            return _ctx_cache[cache_key]

        # Verify user membership in this tenant
        user_q = (
            sa.select(User)
            .join(UserTenant, UserTenant.user_id == User.id)
            .where(
                User.keycloak_sub == keycloak_sub,
                UserTenant.tenant_id == tenant_id,
                User.is_active == True,
            )
        )
        user: User | None = (await self._session.execute(user_q)).scalar_one_or_none()
        if user is None:
            return None

        # Upsert user profile (sync display_name/email from Keycloak)
        if user.email != email or user.display_name != display_name:
            user.email = email
            user.display_name = display_name
            await self._session.flush()

        # Fetch permissions for this tenant
        perm_q = (
            sa.select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .join(Role, Role.id == UserRole.role_id)
            .where(
                UserRole.user_id == user.id,
                Role.tenant_id == tenant_id,
            )
        )
        permission_codes: list[str] = list(
            (await self._session.execute(perm_q)).scalars().all()
        )

        # Fetch role names
        role_q = (
            sa.select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id, Role.tenant_id == tenant_id)
        )
        role_names: list[str] = list(
            (await self._session.execute(role_q)).scalars().all()
        )

        # Fetch collection access (read and write separately)
        access_q = (
            sa.select(CollectionAccess.collection_id, CollectionAccess.access_level)
            .join(UserRole, UserRole.role_id == CollectionAccess.role_id)
            .join(Role, Role.id == UserRole.role_id)
            .join(Collection, Collection.id == CollectionAccess.collection_id)
            .where(
                UserRole.user_id == user.id,
                Role.tenant_id == tenant_id,
                Collection.is_active == True,
                Collection.tenant_id == tenant_id,  # Explicit tenant filter
            )
        )
        access_rows = (await self._session.execute(access_q)).all()

        readable = frozenset(row.collection_id for row in access_rows)
        writable = frozenset(
            row.collection_id for row in access_rows if row.access_level == "write"
        )

        ctx = UserContext(
            user_id=user.id,
            keycloak_sub=keycloak_sub,
            email=email,
            display_name=display_name,
            tenant_id=tenant_id,
            roles=frozenset(role_names),
            permissions=frozenset(permission_codes),
            allowed_collection_ids=readable,
            writable_collection_ids=writable,
        )
        _ctx_cache[cache_key] = ctx
        return ctx
```

### Cache Invalidation

The `_ctx_cache` TTL is 60 seconds. When an admin changes a user's role or collection access, the change takes effect within 60 seconds without any cache invalidation action. For immediate revocation (e.g., security incident), the `invalidate_user_context_cache(keycloak_sub, tenant_id)` function must be called by the role management service:

```python
def invalidate_user_context_cache(keycloak_sub: str, tenant_id: UUID) -> None:
    """Called by TenantService after role changes. Invalidates the 60s cache."""
    _ctx_cache.pop((keycloak_sub, tenant_id), None)
```

## Implementation Steps

1. **Create `src/domain/auth.py`** — `UserContext` frozen dataclass as shown (canonical definition). **Create `src/api/schemas/auth.py`** — re-export only: `from src.domain.auth import UserContext as UserContext`. Routers may import from either path; `AuthRepository` and all domain/db code must import from `src.domain.auth`.

2. **Create `src/core/clients/jwks.py`** — async JWKS client with `TTLCache` and stale-on-failure logic

3. **Create `src/db/repositories/auth_repository.py`** — `AuthRepository.build_user_context()` with in-process cache

4. **Create `src/api/dependencies/__init__.py`** and `src/api/dependencies/auth.py`** — all dependencies as shown

5. **Update `src/api/exception_handlers.py`** — ensure `TenantIsolationError` maps to 403 with a generic message (not the exception `str()` which would leak the resource ID)

6. **Create `src/api/dependencies/pagination.py`** — shared pagination dependency used by list endpoints:
   ```python
   from pydantic import BaseModel, Field

   class PaginationParams(BaseModel):
       page: int = Field(default=1, ge=1)
       page_size: int = Field(default=20, ge=1, le=100)

       @property
       def offset(self) -> int:
           return (self.page - 1) * self.page_size
   ```

7. **Write unit tests** (see Tests section)

8. **Write security tests** (see Tests section)

9. **Run:** `ruff check --fix src/api/dependencies/ src/core/clients/ src/db/repositories/auth_repository.py && mypy src/`

10. **Run `/tenant-isolation-check` skill** — verify isolation tests pass before marking done

## Security Checklist

- JWT signature algorithm is hardcoded to `RS256` — no `none` algorithm, no HS256 (would allow token forgery)
- `exp` claim is always validated; `jwt.decode()` options explicitly set `verify_exp: True`
- `aud` claim is validated against `settings.KEYCLOAK_AUDIENCE` — tokens for other services are rejected
- `tenant_id` is ALWAYS from JWT claims, never from request body/query string
- JWKS fetch has timeout (connect=3s, read=5s per §16); served stale on failure
- `TenantIsolationError` maps to `403` with message `"Access denied"` — no resource details leaked
- Cache key is `(keycloak_sub, tenant_id)` — one user's cache cannot bleed into another tenant
- `UserContext` is frozen (immutable) — cannot be accidentally mutated in handlers
- Logging in auth layer: only `user_id`, `error_type`, `permission_code` — never email, JWT payload content, or sub claim value
- The `keycloak_sub` in logs is acceptable (it is an opaque identifier, not PII per GDPR in this context); email is NOT logged
- JWKS cache lock prevents thundering herd on cold start

## Terms of Use (relevant constraints)

- Keycloak is the single source of truth for authentication. No local password storage. No bypass auth endpoints.
- Role and permission data in Postgres is the source of truth for authorization. Keycloak roles are for coarse-grained access (realm role = `contributor`); fine-grained permissions come from the `permissions` table.
- Medical context: a failed permission check on a Viewer trying to delete a document is an expected event. A failed tenant isolation check is a security event and must be logged at `ERROR` level with the `tenant_isolation_violation` event key.
- Session cache TTL of 60 seconds means role changes are eventually consistent. This is acceptable for the medical use case (no real-time revocation requirement). For immediate revocation, call `invalidate_user_context_cache()`.

## Tests

References TASK-016 `tests/unit/auth/` and `tests/security/`.

**`tests/unit/auth/test_get_current_ctx.py`**

```python
# Fixture:
def make_jwt(payload: dict, secret: str = "test-secret", algorithm: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)
```

Test cases:
- Valid JWT with all required claims → returns `UserContext` with correct fields
- JWT with expired `exp` → raises `HTTP 401`
- JWT with wrong `aud` → raises `HTTP 401`
- JWT with missing `tenant_id` claim → raises `HTTP 401`
- JWT with invalid `tenant_id` (not a UUID) → raises `HTTP 401`
- JWT with valid signature but user not in `user_tenants` → raises `HTTP 403`
- Inactive user (`is_active=False`) → raises `HTTP 403`
- Second call within 60s returns cached context (no DB query)
- RS256 token with wrong key → raises `HTTP 401`

**`tests/unit/auth/test_require_permission.py`**

- User with `documents:upload` permission → dependency passes
- User without `documents:upload` → raises `HTTP 403`
- Empty permissions frozenset → raises `HTTP 403` for any permission check

**`tests/unit/auth/test_get_tenant_collections.py`**

- User with `read` access to collection A → `allowed_collection_ids` contains A
- User with `write` access to collection B → both `allowed_collection_ids` and `writable_collection_ids` contain B
- Inactive collection → not included in allowed or writable sets
- Collection from different tenant → not included (collection's `tenant_id` filter enforced)

**`tests/security/test_jwt.py`** (mark: `@pytest.mark.auth`)

- `none` algorithm rejected
- HS256 token rejected (server requires RS256)
- Token signed with wrong RSA key → 401
- Token with `exp` in past → 401
- Token with future `iat` (clock skew) → handled gracefully
- Token without `aud` → 401
- Token with wrong `aud` (`"not-rag-api"`) → 401

**`tests/security/test_rbac.py`** (mark: `@pytest.mark.auth`)

- Viewer cannot call `POST /documents` (lacks `documents:upload`)
- Contributor can call `POST /documents`
- Admin can call `DELETE /documents/{id}`
- Owner can call all endpoints
- Cross-tenant: token for tenant A rejected on tenant B's resources (403, not 404)

**`tests/security/test_tenant_isolation.py`** (mark: `@pytest.mark.tenant_isolation`)

- User in tenant A cannot read tenant B's document (ID guessing → 403)
- User in tenant A cannot read tenant B's collections
- `assert_tenant_owns_resource()` raises `TenantIsolationError` when `resource_tenant_id != ctx.tenant_id`
- `TenantIsolationError` is always mapped to HTTP 403, never 404

## Definition of Done

- [ ] `UserContext` frozen dataclass with all required fields implemented
- [ ] JWKS client with async fetch, 5-minute TTL cache, stale-on-failure behavior
- [ ] `get_current_ctx` dependency validates RS256 JWT, exp, aud, tenant_id claim
- [ ] `require_permission()` dependency factory returning per-permission check
- [ ] `assert_tenant_owns_resource()` function raises `TenantIsolationError` on mismatch
- [ ] `AuthRepository.build_user_context()` with 60-second in-process cache
- [ ] Cache invalidation function `invalidate_user_context_cache()` available
- [ ] `TenantIsolationError` → HTTP 403 with generic message (no resource details)
- [ ] All unit auth tests pass
- [ ] All `@pytest.mark.auth` tests pass
- [ ] All `@pytest.mark.tenant_isolation` tests pass
- [ ] `mypy src/api/dependencies/ src/core/clients/ src/db/repositories/` exits zero
- [ ] `ruff check` exits zero
- [ ] Security auditor sign-off on the tenant isolation implementation
