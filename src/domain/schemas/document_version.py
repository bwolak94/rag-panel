"""Pydantic schemas for document versioning API (TASK-025)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentVersionItem(BaseModel):
    id: uuid.UUID
    version_number: int
    is_current: bool
    status: str
    sha256: str
    created_at: datetime
    note: str | None = None


class DocumentVersionListResponse(BaseModel):
    document_id: uuid.UUID
    versions: list[DocumentVersionItem]


class DocumentVersionConflictResponse(BaseModel):
    conflict: str = "document_exists"
    existing_document_id: uuid.UUID
    existing_version: int
    message: str
    actions: list[str] = ["new_version", "new_document"]
