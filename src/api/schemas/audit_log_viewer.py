"""Pydantic schemas for the audit log viewer API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditLogItem(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    user_display_name: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogItem]
    total_count: int
    next_cursor: str | None = None
    has_next_page: bool


class AuditLogActionsResponse(BaseModel):
    actions: list[str]
