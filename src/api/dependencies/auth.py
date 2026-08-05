"""FastAPI auth/RBAC dependencies.

Every protected endpoint must declare `get_current_ctx` plus appropriate permission guards.
`tenant_id` is ALWAYS from JWT claims — never from request body or query string.

Domain-level checks (require_collection_read/write, assert_tenant_owns_resource) are
re-exported from src.domain.auth so both API routes and RetrievalService can import them
from a single, consistent location without crossing layer boundaries.
"""

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose.exceptions import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.jwks import get_public_keys, verify_token
from src.core.database import get_db_session
from src.core.exceptions import AuthenticationError
from src.db.repositories.auth_repository import AuthRepository
from src.domain.auth import UserContext
from src.domain.auth import assert_tenant_owns_resource as assert_tenant_owns_resource
from src.domain.auth import require_collection_read as require_collection_read
from src.domain.auth import require_collection_write as require_collection_write

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=True)

__all__ = [
    "assert_tenant_owns_resource",
    "get_current_ctx",
    "require_collection_read",
    "require_collection_write",
    "require_permission",
    "require_realm_role",
]


async def get_current_ctx(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> UserContext:
    """Primary auth dependency.

    Validates JWT (RS256, exp, aud, tenant_id claim), resolves user+tenant from DB,
    and returns an immutable UserContext.

    Raises:
        HTTPException(401): token missing, expired, invalid signature, or missing claims.
        HTTPException(403): user not a member of the tenant in token.
        HTTPException(503): Keycloak JWKS unavailable and no stale keys available.
    """
    token = credentials.credentials
    try:
        public_keys = await get_public_keys()
        claims = verify_token(token, public_keys)
    except JWTError as exc:
        # Normal auth failures: expired, wrong signature, wrong aud, missing aud
        logger.warning("jwt_validation_failed", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthenticationError as exc:
        # Infrastructure failure: Keycloak unreachable with no stale keys
        logger.error("jwks_unavailable", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc

    # Extract claims — NEVER from body/query
    keycloak_sub: str = claims["sub"]
    tenant_id_str: str | None = claims.get("tenant_id")
    email: str = claims.get("email", "")
    display_name: str = claims.get("name", "")
    realm_roles: frozenset[str] = frozenset(claims.get("realm_access", {}).get("roles", []))

    if not tenant_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing tenant_id claim",
        )

    try:
        tenant_id = UUID(tenant_id_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tenant_id in token",
        ) from exc

    repo = AuthRepository(session)
    user_ctx = await repo.build_user_context(
        keycloak_sub=keycloak_sub,
        tenant_id=tenant_id,
        email=email,
        display_name=display_name,
    )
    if user_ctx is None:
        # User is not a member of this tenant — 403, not 404 (no existence leak)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Attach JWT realm roles (Keycloak realm_access.roles) — not stored in DB,
    # evaluated per request from the validated token.
    if realm_roles:
        user_ctx = dataclasses.replace(user_ctx, realm_roles=realm_roles)

    return user_ctx


def require_permission(permission_code: str) -> Callable[..., Awaitable[None]]:
    """Return a FastAPI dependency that raises 403 if the user lacks the permission.

    Usage:
        _: Annotated[None, Depends(require_permission("documents:upload"))]
    """

    async def _check(ctx: Annotated[UserContext, Depends(get_current_ctx)]) -> None:
        if not ctx.has_permission(permission_code):
            logger.warning(
                "permission_denied",
                user_id=str(ctx.user_id),
                permission=permission_code,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

    return _check


def require_realm_role(role_name: str) -> Callable[..., Awaitable[None]]:
    """Return a FastAPI dependency that raises 403 if the user lacks the JWT realm role.

    Realm roles come from Keycloak's realm_access.roles claim — they are NOT
    stored in the application DB. Use this for platform-level roles (e.g. platform:admin)
    that span all tenants, as opposed to `require_permission` for tenant-scoped DB permissions.

    Usage:
        _: Annotated[None, Depends(require_realm_role("platform:admin"))]
    """

    async def _check(ctx: Annotated[UserContext, Depends(get_current_ctx)]) -> None:
        if role_name not in ctx.realm_roles:
            logger.warning(
                "realm_role_denied",
                user_id=str(ctx.user_id),
                required_role=role_name,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )

    return _check


# NOTE: require_collection_read, require_collection_write, assert_tenant_owns_resource
# are imported from src.domain.auth and re-exported above via explicit `as X` aliases.
# This keeps them importable from src.api.dependencies.auth (for API layer tests)
# AND from src.domain.auth (for RetrievalService and graph nodes) without duplication.
