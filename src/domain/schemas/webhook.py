"""Pydantic schemas for the webhook management API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ── Request schemas ───────────────────────────────────────────────────────────


class RegisterWebhookRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=8, max_length=2048)
    events: list[str] = Field(..., min_length=1)

    @field_validator("url")
    @classmethod
    def url_must_be_https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Webhook URL must use HTTPS")
        return v

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        allowed = {
            "document.uploaded",
            "document.ready",
            "document.failed",
            "document.needs_review",
            "document.approved",
            "document.rejected",
            "ingestion_job.started",
            "ingestion_job.completed",
        }
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"Unknown event types: {sorted(unknown)}")
        return list(set(v))


class UpdateWebhookRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    events: list[str] | None = None
    is_active: bool | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        allowed = {
            "document.uploaded",
            "document.ready",
            "document.failed",
            "document.needs_review",
            "document.approved",
            "document.rejected",
            "ingestion_job.started",
            "ingestion_job.completed",
        }
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"Unknown event types: {sorted(unknown)}")
        return list(set(v))


# ── Response schemas ──────────────────────────────────────────────────────────


class WebhookResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    url: str
    secret: str  # "***" after creation
    events: list[str]
    is_active: bool
    failure_count: int
    last_triggered_at: datetime | None
    created_at: datetime
    created_by: uuid.UUID | None


class WebhookCreatedResponse(BaseModel):
    """Returned only at creation — includes the plaintext secret once."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    url: str
    secret: str  # plaintext — only shown at creation
    events: list[str]
    is_active: bool
    created_at: datetime


class WebhookDeliveryResponse(BaseModel):
    id: uuid.UUID
    webhook_id: uuid.UUID
    event_type: str
    status: str
    http_status: int | None
    attempt_count: int
    next_retry_at: datetime | None
    created_at: datetime
    delivered_at: datetime | None


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    total: int


class WebhookListResponse(BaseModel):
    items: list[WebhookResponse]
    total: int


class WebhookTestResponse(BaseModel):
    delivery_id: uuid.UUID
    status: str
    message: str


class WebhookDispatchData(BaseModel):
    """Internal DTO — data attached to a webhook event payload."""

    model_config = {"extra": "allow"}

    document_id: uuid.UUID | None = None
    document_name: str | None = None
    collection_id: uuid.UUID | None = None
    collection_name: str | None = None
    status: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
