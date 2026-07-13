"""AuditService — writes append-only entries to audit_log.

Rules:
- details must contain only IDs and metadata — no PII, no document content.
- Never UPDATE or DELETE from audit_log.
- ip is written as-is into the INET column; pass None if unknown.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.audit_log import AuditLog
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(
        self,
        *,
        ctx: UserContext,
        action: str,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
        ip: str | None = None,
    ) -> None:
        entry = AuditLog(
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip=ip,
            details=details or {},
        )
        self._session.add(entry)
        # Do NOT flush here — caller manages the transaction boundary.
        logger.info(
            "audit_log",
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            tenant_id=str(ctx.tenant_id),
            user_id=str(ctx.user_id),
        )
