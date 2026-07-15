"""Pydantic schemas for ingest Redis Streams events."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


class IngestEvent(BaseModel):
    """
    Canonical event from Redis Streams ingest_events.
    schema_version must be "1" — unknown versions route to DLQ immediately.
    """

    schema_version: str
    event_type: str
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    collection_id: uuid.UUID
    minio_bucket: str
    minio_key: str
    size_bytes: int
    content_type: str
    published_at: datetime

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if v != "1":
            raise ValueError(f"Unknown schema_version: {v}. Expected '1'.")
        return v


class DeadLetterEvent(BaseModel):
    """Event written to ingest_events_dlq after max retries."""

    original_event: dict[str, str]
    failure_reason: str
    failure_count: int
    last_failed_at: datetime
    document_id: uuid.UUID
    tenant_id: uuid.UUID
