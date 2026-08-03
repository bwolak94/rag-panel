"""Pydantic schemas for the conversation export API (TASK-024)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class ExportStartResponse(BaseModel):
    export_id: uuid.UUID
    status: str
    format: str
    status_url: str


class ExportStatusResponse(BaseModel):
    export_id: uuid.UUID
    status: str
    format: str
    download_url: str | None = None
    download_url_expires_at: datetime | None = None
    expires_at: datetime


class GdprExportStartResponse(BaseModel):
    export_id: uuid.UUID
    status: str
    status_url: str
