"""Unit tests for IngestEvent and DeadLetterEvent schemas."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.ingest.schemas import DeadLetterEvent, IngestEvent

_VALID_FIELDS = {
    "schema_version": "1",
    "event_type": "document.uploaded",
    "tenant_id": str(uuid.uuid4()),
    "document_id": str(uuid.uuid4()),
    "collection_id": str(uuid.uuid4()),
    "minio_bucket": "tenant-clinic",
    "minio_key": "raw/abc/def/report.pdf",
    "size_bytes": "1024",
    "content_type": "application/pdf",
    "published_at": datetime.now(timezone.utc).isoformat(),
}


def test_valid_ingest_event_parses() -> None:
    event = IngestEvent(**_VALID_FIELDS)
    assert event.schema_version == "1"
    assert isinstance(event.document_id, uuid.UUID)


def test_unknown_schema_version_raises_value_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        IngestEvent(**{**_VALID_FIELDS, "schema_version": "2"})
    assert "Unknown schema_version" in str(exc_info.value)


def test_invalid_document_id_raises() -> None:
    with pytest.raises(ValidationError):
        IngestEvent(**{**_VALID_FIELDS, "document_id": "not-a-uuid"})


def test_missing_required_field_raises() -> None:
    fields = dict(_VALID_FIELDS)
    del fields["minio_key"]
    with pytest.raises(ValidationError):
        IngestEvent(**fields)


def test_dead_letter_event_instantiates() -> None:
    doc_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    dlq = DeadLetterEvent(
        original_event={"key": "value"},
        failure_reason="max_retries_exceeded",
        failure_count=3,
        last_failed_at=datetime.now(timezone.utc),
        document_id=doc_id,
        tenant_id=tenant_id,
    )
    assert dlq.failure_count == 3
    assert dlq.document_id == doc_id
