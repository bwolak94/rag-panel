"""Admin quota management router.

GET   /api/v1/admin/quota/status              — current quota usage for tenant
PATCH /api/v1/platform/tenants/{id}/quotas    — update quota limits (platform-admin)

Security:
- GET requires admin:analytics permission (reuses existing grant).
- PATCH requires platform:admin role (platform-admin only).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.quota import QuotaStatusResponse, UpdateQuotasRequest
from src.core.clients.redis_client import get_redis_client
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.quota_service import QuotaService

router = APIRouter(tags=["admin-quota"])

_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]
_RequireAdmin = Annotated[None, Depends(require_permission("admin:analytics"))]
_RequirePlatformAdmin = Annotated[None, Depends(require_permission("platform:admin"))]


@router.get(
    "/api/v1/admin/quota/status",
    response_model=QuotaStatusResponse,
    summary="Current quota usage for the tenant",
)
async def get_quota_status(
    ctx: _Ctx,
    _: _RequireAdmin,
    session: _Session,
) -> QuotaStatusResponse:
    return await QuotaService(session, get_redis_client()).get_quota_status(ctx.tenant_id)


@router.patch(
    "/api/v1/platform/tenants/{tenant_id}/quotas",
    status_code=204,
    summary="Update quota limits for a tenant (platform-admin only)",
)
async def update_tenant_quotas(
    tenant_id: uuid.UUID,
    body: UpdateQuotasRequest,
    ctx: _Ctx,
    _: _RequirePlatformAdmin,
    session: _Session,
) -> None:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    await QuotaService(session, get_redis_client()).update_tenant_quotas(tenant_id, updates)
