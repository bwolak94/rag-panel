"""Conversation export and GDPR data portability router.

POST /api/v1/conversations/{id}/export        — start export (json|pdf)
GET  /api/v1/exports/{id}/status              — poll export status + download URL
POST /api/v1/users/me/export                  — GDPR all-conversations ZIP

Security:
- Users can export only their own conversations.
- Admins can export any conversation within their tenant.
- Presigned URL TTL = 60 s; export file expires after 1 h.
- Admin exporting another user's conversation is audit-logged by the service.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.api.schemas.export import (
    ExportStartResponse,
    ExportStatusResponse,
    GdprExportStartResponse,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.export_service import ExportService

router = APIRouter(prefix="/api/v1", tags=["exports"])

_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]

_ALLOWED_FORMATS = {"json", "pdf"}


@router.post(
    "/conversations/{conversation_id}/export",
    response_model=ExportStartResponse,
    status_code=202,
    summary="Start async export of a conversation (JSON or PDF)",
)
async def start_conversation_export(
    conversation_id: uuid.UUID,
    ctx: _Ctx,
    session: _Session,
    format: str = Query(default="json", pattern="^(json|pdf)$"),
) -> ExportStartResponse:
    return await ExportService(session).create_conversation_export(
        conversation_id=conversation_id,
        fmt=format,
        ctx=ctx,
    )


@router.get(
    "/exports/{export_id}/status",
    response_model=ExportStatusResponse,
    summary="Poll export status; download URL included when ready (TTL 60 s)",
)
async def get_export_status(
    export_id: uuid.UUID,
    ctx: _Ctx,
    session: _Session,
) -> ExportStatusResponse:
    return await ExportService(session).get_export_status(export_id, ctx)


@router.post(
    "/users/me/export",
    response_model=GdprExportStartResponse,
    status_code=202,
    summary="GDPR Art. 20 — export all your conversations as a ZIP of JSON files",
)
async def gdpr_export(
    ctx: _Ctx,
    session: _Session,
) -> GdprExportStartResponse:
    return await ExportService(session).create_gdpr_export(ctx)
