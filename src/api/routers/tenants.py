"""Tenant Management API router.

Privileged operations (create/delete tenant, manage users) require specific permissions.
`tenant_id` is ALWAYS from JWT context or path param validated against ctx.tenant_id —
never from request body.

Security invariants:
- `assert_tenant_owns_resource(tenant_id, ctx)` is the FIRST call in every handler
  that operates on an existing tenant (exceptions: POST /tenants — platform-admin creates
  a new tenant; may not be a member yet).
- Write operations commit the session after the service call.
- Cache invalidation for role changes is handled by TenantService.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import (
    assert_tenant_owns_resource,
    get_current_ctx,
    require_permission,
)
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.schemas.tenant import (
    AddUserRequest,
    AssignRoleRequest,
    TenantCreate,
    TenantMemberResponse,
    TenantResponse,
    TenantUpdate,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.tenant_service import TenantService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/tenants", tags=["tenants"])

_require_admin_tenants = require_permission("admin:tenants")
_require_admin_users = require_permission("admin:users")


def _get_service(session: AsyncSession) -> TenantService:
    return TenantService(session)


# ── Tenant CRUD ─────────────────────────────────────────────────────────────


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=TenantResponse,
    summary="Create a new tenant (platform-admin only)",
)
async def create_tenant(
    body: TenantCreate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_tenants)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantResponse:
    # NOTE: No assert_tenant_owns_resource here — platform-admin creates tenants
    # they may not yet belong to. Permission check via require_permission("admin:tenants").
    ip = request.client.host if request.client else None
    result = await _get_service(session).create_tenant(body, ctx, ip)
    await session.commit()
    return result


@router.get(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Get tenant details (own tenant only)",
)
async def get_tenant(
    tenant_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantResponse:
    assert_tenant_owns_resource(tenant_id, ctx)
    return await _get_service(session).get_tenant(tenant_id)


@router.patch(
    "/{tenant_id}",
    response_model=TenantResponse,
    summary="Update tenant name or settings",
)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_tenants)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantResponse:
    assert_tenant_owns_resource(tenant_id, ctx)
    ip = request.client.host if request.client else None
    result = await _get_service(session).update_tenant(tenant_id, body, ctx, ip)
    await session.commit()
    return result


@router.delete(
    "/{tenant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a tenant (platform-admin only)",
)
async def delete_tenant(
    tenant_id: uuid.UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_tenants)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    assert_tenant_owns_resource(tenant_id, ctx)
    ip = request.client.host if request.client else None
    await _get_service(session).delete_tenant(tenant_id, ctx, ip)
    await session.commit()


# ── User membership management ───────────────────────────────────────────────


@router.get(
    "/{tenant_id}/users",
    response_model=list[TenantMemberResponse],
    summary="List users in this tenant (admin:users only)",
)
async def list_members(
    tenant_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_users)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> list[TenantMemberResponse]:
    assert_tenant_owns_resource(tenant_id, ctx)
    members, _total = await _get_service(session).list_members(
        tenant_id,
        offset=pagination.offset,
        limit=pagination.page_size,
    )
    return members


@router.post(
    "/{tenant_id}/users",
    status_code=status.HTTP_201_CREATED,
    response_model=TenantMemberResponse,
    summary="Add a user to this tenant by keycloak_sub",
)
async def add_user(
    tenant_id: uuid.UUID,
    body: AddUserRequest,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_users)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantMemberResponse:
    assert_tenant_owns_resource(tenant_id, ctx)
    ip = request.client.host if request.client else None
    result = await _get_service(session).add_user(tenant_id, body, ctx, ip)
    await session.commit()
    return result


@router.delete(
    "/{tenant_id}/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a user from this tenant (also removes user_roles)",
)
async def remove_user(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_users)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    assert_tenant_owns_resource(tenant_id, ctx)
    ip = request.client.host if request.client else None
    await _get_service(session).remove_user(tenant_id, user_id, ctx, ip)
    await session.commit()


@router.put(
    "/{tenant_id}/users/{user_id}/role",
    response_model=TenantMemberResponse,
    summary="Assign a role to a user within this tenant",
)
async def assign_role(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    body: AssignRoleRequest,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin_users)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TenantMemberResponse:
    assert_tenant_owns_resource(tenant_id, ctx)
    ip = request.client.host if request.client else None
    result = await _get_service(session).assign_role(tenant_id, user_id, body, ctx, ip)
    await session.commit()
    return result
