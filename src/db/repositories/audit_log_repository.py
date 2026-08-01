"""Read-only repository for the audit_log table.

Security:
- Every query is filtered by tenant_id from UserContext — never from body/query params.
- NEVER issue UPDATE or DELETE against audit_log.
- ip_address column is included — callers must handle with care (potential PII).
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.audit_log import AuditLog
from src.db.models.user import User


class AuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_events(
        self,
        tenant_id: uuid.UUID,
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
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (items, has_next_page).

        Cursor format: base64(iso_created_at + ":" + id)
        has_next_page is True when there are more items beyond this page.
        """
        # Decode cursor
        cursor_ts: datetime | None = None
        cursor_id: uuid.UUID | None = None
        if cursor:
            try:
                decoded = base64.b64decode(cursor.encode()).decode()
                ts_str, id_str = decoded.rsplit(":", 1)
                cursor_ts = datetime.fromisoformat(ts_str)
                cursor_id = uuid.UUID(id_str)
            except Exception:
                pass  # invalid cursor → start from beginning

        q = (
            select(
                AuditLog.id,
                AuditLog.user_id,
                User.display_name.label("user_display_name"),
                AuditLog.action,
                AuditLog.resource_type,
                AuditLog.resource_id,
                AuditLog.details,
                AuditLog.ip.label("ip_address"),
                AuditLog.created_at,
            )
            .outerjoin(User, AuditLog.user_id == User.id)
            .where(AuditLog.tenant_id == tenant_id)
        )

        if action:
            q = q.where(AuditLog.action == action)
        elif action_prefix:
            q = q.where(AuditLog.action.like(f"{action_prefix}%"))
        if user_id:
            q = q.where(AuditLog.user_id == user_id)
        if resource_type:
            q = q.where(AuditLog.resource_type == resource_type)
        if resource_id:
            q = q.where(AuditLog.resource_id == resource_id)
        if from_date:
            q = q.where(AuditLog.created_at >= from_date)
        if to_date:
            q = q.where(AuditLog.created_at <= to_date)

        if cursor_ts and cursor_id:
            q = q.where(
                (AuditLog.created_at < cursor_ts)
                | (
                    (AuditLog.created_at == cursor_ts)
                    & (AuditLog.id < cursor_id)
                )
            )

        q = q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(page_size + 1)

        rows = list((await self._session.execute(q)).mappings().all())
        has_next = len(rows) > page_size
        items = rows[:page_size]
        return [dict(r) for r in items], has_next

    async def count_events(
        self,
        tenant_id: uuid.UUID,
        *,
        action: str | None = None,
        action_prefix: str | None = None,
        user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> int:
        """Return the total count matching the given filters."""
        q = select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == tenant_id)
        if action:
            q = q.where(AuditLog.action == action)
        elif action_prefix:
            q = q.where(AuditLog.action.like(f"{action_prefix}%"))
        if user_id:
            q = q.where(AuditLog.user_id == user_id)
        if resource_type:
            q = q.where(AuditLog.resource_type == resource_type)
        if resource_id:
            q = q.where(AuditLog.resource_id == resource_id)
        if from_date:
            q = q.where(AuditLog.created_at >= from_date)
        if to_date:
            q = q.where(AuditLog.created_at <= to_date)
        return (await self._session.execute(q)).scalar_one()

    async def get_distinct_actions(self, tenant_id: uuid.UUID) -> list[str]:
        """Return sorted list of distinct action values for this tenant."""
        q = (
            select(distinct(AuditLog.action))
            .where(AuditLog.tenant_id == tenant_id)
            .order_by(AuditLog.action)
        )
        rows = (await self._session.execute(q)).scalars().all()
        return list(rows)

    @staticmethod
    def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
        raw = f"{created_at.isoformat()}:{row_id}"
        return base64.b64encode(raw.encode()).decode()
