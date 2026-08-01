"""Audit log viewer router.

GET  /api/v1/admin/audit-log           → AuditLogListResponse (paginated, filterable)
GET  /api/v1/admin/audit-log/export    → text/csv download (max 10 000 rows)
GET  /api/v1/admin/audit-log/actions   → AuditLogActionsResponse (distinct action list)

All endpoints require permission 'admin:audit_log'.
tenant_id is ALWAYS from JWT context — never from body/query/path.

Route order: /audit-log/export and /audit-log/actions must appear before
/audit-log to prevent path conflicts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.audit_log_viewer import AuditLogActionsResponse, AuditLogListResponse
from src.core.database import get_db_session
from src.domain.audit_log_viewer_service import AuditLogViewerService
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

router = APIRouter(prefix="/api/v1/admin", tags=["audit-log"])

_RequireAudit = Annotated[None, Depends(require_permission("admin:audit_log"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


def _get_service(session: AsyncSession) -> AuditLogViewerService:
    return AuditLogViewerService(session)


@router.get(
    "/audit-log/actions",
    response_model=AuditLogActionsResponse,
    summary="List distinct audit event action types for this tenant",
)
async def get_audit_actions(
    ctx: _Ctx,
    _: _RequireAudit,
    session: _Session,
) -> AuditLogActionsResponse:
    return await _get_service(session).get_distinct_actions(ctx)


@router.get(
    "/audit-log/export",
    summary="Export audit events as CSV (max 10 000 rows)",
)
async def export_audit_log(
    ctx: _Ctx,
    _: _RequireAudit,
    session: _Session,
    action: str | None = None,
    action_prefix: str | None = None,
    user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> StreamingResponse:
    svc = _get_service(session)
    audit_svc = AuditService(session)
    csv_content = await svc.export_csv(
        ctx,
        audit_svc,
        action=action,
        action_prefix=action_prefix,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        from_date=from_date,
        to_date=to_date,
    )
    await session.commit()
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )


@router.get(
    "/audit-log",
    response_model=AuditLogListResponse,
    summary="List audit events with filtering and cursor-based pagination",
)
async def list_audit_events(
    ctx: _Ctx,
    _: _RequireAudit,
    session: _Session,
    action: str | None = None,
    action_prefix: str | None = None,
    user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditLogListResponse:
    return await _get_service(session).list_events(
        ctx,
        action=action,
        action_prefix=action_prefix,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        from_date=from_date,
        to_date=to_date,
        cursor=cursor,
        page_size=page_size,
    )
