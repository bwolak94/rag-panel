"""Terms of Service REST endpoints.

Endpoints:
  GET  /terms                           — public, no auth required
  POST /tenants/{tenant_id}/terms/accept — requires Owner role
  GET  /tenants/{tenant_id}/terms/status — requires Owner or Admin role
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.api.schemas.tos import (
    TosAcceptanceRecord,
    TosAcceptRequest,
    TosStatusResponse,
    TosVersionPublic,
)
from src.core.clients.redis_client import get_redis_client
from src.core.database import get_db_session
from src.core.exceptions import PermissionDeniedError
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext, assert_tenant_owns_resource
from src.domain.mau_service import MauService
from src.domain.tos_service import TosService

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["terms"])


def _mask_ip(ip: str) -> str:
    """Mask the last octet of an IPv4 address or last segment of IPv6.

    Args:
        ip: Raw IP address string.

    Returns:
        Masked IP string (e.g. "192.168.1.xxx" or "2001:db8::xxx").
    """
    if ":" in ip:  # IPv6
        parts = ip.rsplit(":", 1)
        return parts[0] + ":xxx"
    parts = ip.rsplit(".", 1)
    if len(parts) == 2:
        return parts[0] + ".xxx"
    return ip


# ── GET /terms ─────────────────────────────────────────────────────────────


@router.get(
    "/terms",
    response_model=TosVersionPublic,
    summary="Get the currently active Terms of Service",
)
async def get_current_tos(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TosVersionPublic:
    """Return the active ToS version. No authentication required."""
    redis = get_redis_client()
    svc = TosService(session, redis)
    tos_version = await svc.get_current_tos()
    return TosVersionPublic.model_validate(tos_version)


# ── POST /tenants/{tenant_id}/terms/accept ─────────────────────────────────


@router.post(
    "/tenants/{tenant_id}/terms/accept",
    response_model=TosAcceptanceRecord,
    status_code=status.HTTP_200_OK,
    summary="Accept the current Terms of Service (Owner only)",
)
async def accept_tos(
    tenant_id: uuid.UUID,
    body: TosAcceptRequest,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TosAcceptanceRecord:
    """Record consent for the current ToS version.

    Only the tenant Owner is allowed to accept on behalf of the organisation.
    The acceptance is idempotent: calling again returns the existing record.
    """
    assert_tenant_owns_resource(tenant_id, ctx)

    if "owner" not in {r.lower() for r in ctx.roles}:
        raise PermissionDeniedError("Only tenant owners can accept the Terms of Service")

    # Prefer X-Forwarded-For (set by reverse proxy); fall back to direct connection host
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        raw_ip = forwarded_for.split(",")[0].strip()
    else:
        raw_ip = request.client.host if request.client else "unknown"

    user_agent = request.headers.get("User-Agent")

    redis = get_redis_client()
    svc = TosService(session, redis)

    acceptance = await svc.accept_tos(
        tenant_id=tenant_id,
        user_id=ctx.user_id,
        tos_version_id=body.tos_version_id,
        ip_address=raw_ip,
        user_agent=user_agent,
        explicit_consent=body.explicit_consent,
    )

    # Resolve version string for the response (acceptance.tos_version_id links to active version)
    active_version = await svc.get_current_tos()

    # Audit log — IP stored unmasked for legal compliance
    audit = AuditService(session)
    await audit.log(
        ctx=ctx,
        action="tos.accepted",
        resource_type="tos_version",
        resource_id=active_version.id,
        details={
            "version": active_version.version,
            "explicit_consent": True,
        },
        ip=raw_ip,
    )

    await session.commit()

    logger.info(
        "tos.accept_endpoint",
        tenant_id=str(tenant_id),
        tos_version=active_version.version,
    )

    return TosAcceptanceRecord(
        id=acceptance.id,
        tenant_id=acceptance.tenant_id,
        accepted_by_user_id=acceptance.user_id,
        accepted_by_display_name=ctx.display_name,
        tos_version_id=acceptance.tos_version_id,
        tos_version=active_version.version,
        accepted_at=acceptance.accepted_at,
        ip_address=_mask_ip(raw_ip),
    )


# ── GET /tenants/{tenant_id}/terms/status ──────────────────────────────────


@router.get(
    "/tenants/{tenant_id}/terms/status",
    response_model=TosStatusResponse,
    summary="Get ToS acceptance status for a tenant (Owner or Admin)",
)
async def get_tos_status(
    tenant_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TosStatusResponse:
    """Return the current ToS acceptance status and MAU metrics for the tenant."""
    assert_tenant_owns_resource(tenant_id, ctx)

    lower_roles = {r.lower() for r in ctx.roles}
    if "owner" not in lower_roles and "admin" not in lower_roles:
        raise PermissionDeniedError("Only tenant owners or admins can view ToS status")

    redis = get_redis_client()
    tos_svc = TosService(session, redis)
    mau_svc = MauService(session, redis)

    status_dict = await tos_svc.get_tos_status(tenant_id)
    mau_dict = await mau_svc.get_mau_status(tenant_id)

    return TosStatusResponse(
        tenant_id=tenant_id,
        current_tos_version=status_dict["current_tos_version"],
        is_accepted=status_dict["is_accepted"],
        accepted_at=status_dict["accepted_at"],
        accepted_by=status_dict["accepted_by"],
        requires_reacceptance=status_dict["requires_reacceptance"],
        ui_message=status_dict["ui_message"],
        mau_count=mau_dict["count"],
        mau_warning=mau_dict["warning"],
    )
