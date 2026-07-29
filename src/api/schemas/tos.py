"""Pydantic schemas for Terms of Service endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TosVersionPublic(BaseModel):
    """Public representation of an active ToS version."""

    model_config = {"from_attributes": True}

    id: UUID
    version: str
    content: str
    summary: str
    effective_date: datetime
    status: str


class TosAcceptRequest(BaseModel):
    """Request body for accepting the current ToS."""

    tos_version_id: UUID
    explicit_consent: bool


class TosAcceptanceRecord(BaseModel):
    """Response returned after a successful ToS acceptance."""

    id: UUID
    tenant_id: UUID
    accepted_by_user_id: UUID
    accepted_by_display_name: str
    tos_version_id: UUID
    tos_version: str
    accepted_at: datetime
    ip_address: str  # Masked: last octet replaced with 'xxx'


class TosStatusResponse(BaseModel):
    """Current ToS acceptance status for a tenant."""

    tenant_id: UUID
    current_tos_version: str
    is_accepted: bool
    accepted_at: datetime | None = None
    accepted_by: str | None = None
    requires_reacceptance: bool
    ui_message: str | None = None
    mau_count: int | None = None
    mau_warning: bool = False
