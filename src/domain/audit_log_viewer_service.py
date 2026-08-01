"""AuditLogViewerService — read-only access to the audit trail.

Security rules:
- tenant_id is always taken from UserContext (JWT) — never from request params.
- CSV export is itself logged to audit_log (action: "audit_log.exported").
- Never log or expose raw details values — they may contain resource identifiers.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.audit_log_viewer import (
    AuditLogActionsResponse,
    AuditLogItem,
    AuditLogListResponse,
)
from src.db.repositories.audit_log_repository import AuditLogRepository
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)

_MAX_EXPORT_ROWS = 10_000


class AuditLogViewerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AuditLogRepository(session)

    async def list_events(
        self,
        ctx: UserContext,
        *,
        action: str | None = None,
        action_prefix: str | None = None,
        user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> AuditLogListResponse:
        items_raw, has_next = await self._repo.list_events(
            ctx.tenant_id,
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
        total = await self._repo.count_events(
            ctx.tenant_id,
            action=action,
            action_prefix=action_prefix,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            from_date=from_date,
            to_date=to_date,
        )

        items = [_row_to_schema(r) for r in items_raw]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            next_cursor = AuditLogRepository.encode_cursor(last.created_at, last.id)

        return AuditLogListResponse(
            items=items,
            total_count=total,
            next_cursor=next_cursor,
            has_next_page=has_next,
        )

    async def get_distinct_actions(self, ctx: UserContext) -> AuditLogActionsResponse:
        actions = await self._repo.get_distinct_actions(ctx.tenant_id)
        return AuditLogActionsResponse(actions=actions)

    async def export_csv(
        self,
        ctx: UserContext,
        audit_service: Any,
        *,
        action: str | None = None,
        action_prefix: str | None = None,
        user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> str:
        """Return CSV string of matching audit events (max 10 000 rows).

        Also logs "audit_log.exported" to the audit trail (self-auditing).
        """
        items_raw, _ = await self._repo.list_events(
            ctx.tenant_id,
            action=action,
            action_prefix=action_prefix,
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            from_date=from_date,
            to_date=to_date,
            page_size=_MAX_EXPORT_ROWS,
        )

        # Log the export action (self-auditing — required for GDPR accountability)
        await audit_service.log(
            ctx=ctx,
            action="audit_log.exported",
            details={
                "action_filter": action or action_prefix,
                "resource_type_filter": resource_type,
                "row_count": len(items_raw),
            },
        )

        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "id",
                "user_id",
                "user_display_name",
                "action",
                "resource_type",
                "resource_id",
                "details_summary",
                "ip_address",
                "created_at",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in items_raw:
            details: dict[str, Any] = row.get("details") or {}
            details_summary = "; ".join(f"{k}={v}" for k, v in details.items())[:200]
            writer.writerow(
                {
                    "id": row.get("id"),
                    "user_id": row.get("user_id"),
                    "user_display_name": row.get("user_display_name"),
                    "action": row.get("action"),
                    "resource_type": row.get("resource_type"),
                    "resource_id": row.get("resource_id"),
                    "details_summary": details_summary,
                    "ip_address": row.get("ip_address"),
                    "created_at": row.get("created_at"),
                }
            )

        return output.getvalue()


def _row_to_schema(row: dict[str, Any]) -> AuditLogItem:
    return AuditLogItem(
        id=row["id"],
        user_id=row.get("user_id"),
        user_display_name=row.get("user_display_name"),
        action=row["action"],
        resource_type=row.get("resource_type"),
        resource_id=row.get("resource_id"),
        details=row.get("details") or {},
        ip_address=str(row["ip_address"]) if row.get("ip_address") else None,
        created_at=row["created_at"],
    )
