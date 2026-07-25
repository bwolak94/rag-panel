"""Unit tests for IngestWorker and EventProcessor."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ingest.event_processor import (
    MAX_DELAY_S,
    EventProcessor,
    MaxRetriesExceededError,
    compute_backoff,
)
from src.ingest.worker import DLQ_STREAM, STREAM_NAME, IngestWorker

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()

_VALID_FIELDS = {
    "schema_version": "1",
    "event_type": "document.uploaded",
    "tenant_id": str(TENANT_ID),
    "document_id": str(DOCUMENT_ID),
    "collection_id": str(COLLECTION_ID),
    "minio_bucket": "tenant-clinic",
    "minio_key": f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/report.pdf",
    "size_bytes": "1024",
    "content_type": "application/pdf",
    "published_at": datetime.now(UTC).isoformat(),
}


# ── compute_backoff ───────────────────────────────────────────────────────────


def test_compute_backoff_attempt_0_is_approx_2s() -> None:
    """attempt=0: base=2s, result should be in [2.0, 2.0 * 1.2] = [2.0, 2.4]"""
    result = compute_backoff(0)
    assert 2.0 <= result <= 2.4 + 0.01


def test_compute_backoff_capped_at_max() -> None:
    """Large attempt numbers are capped at MAX_DELAY_S."""
    result = compute_backoff(10)
    assert result <= MAX_DELAY_S + 0.01


def test_compute_backoff_increases_on_average() -> None:
    """Higher attempt numbers should produce larger delays on average."""
    samples_0 = [compute_backoff(0) for _ in range(20)]
    samples_3 = [compute_backoff(3) for _ in range(20)]
    assert sum(samples_3) / len(samples_3) > sum(samples_0) / len(samples_0)


# ── EventProcessor ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_processor_unknown_schema_raises() -> None:
    """schema_version != '1' → MaxRetriesExceededError immediately."""
    session = MagicMock()
    processor = EventProcessor()

    with pytest.raises(MaxRetriesExceededError) as exc_info:
        await processor.process(session, {**_VALID_FIELDS, "schema_version": "99"})

    assert "schema_mismatch" in str(exc_info.value)


@pytest.mark.asyncio
async def test_event_processor_already_ready_returns_true() -> None:
    """Document already status=ready → idempotent skip, return True."""
    session = MagicMock()
    doc = MagicMock()
    doc.status = "ready"
    processor = EventProcessor()

    with patch(
        "src.ingest.event_processor.DocumentRepository.get_by_id",
        new=AsyncMock(return_value=doc),
    ):
        result = await processor.process(session, _VALID_FIELDS)

    assert result is True


@pytest.mark.asyncio
async def test_event_processor_max_retries_exceeded_raises() -> None:
    """retry_count >= MAX_RETRIES → raises MaxRetriesExceededError."""
    from src.ingest.event_processor import MAX_RETRIES

    session = MagicMock()
    doc = MagicMock()
    doc.status = "validating"

    job = MagicMock()
    job.retry_count = MAX_RETRIES
    job.id = uuid.uuid4()

    processor = EventProcessor()

    with (
        patch(
            "src.ingest.event_processor.DocumentRepository.get_by_id",
            new=AsyncMock(return_value=doc),
        ),
        patch(
            "src.ingest.event_processor.IngestionJobRepository.get_active_job",
            new=AsyncMock(return_value=job),
        ),pytest.raises(MaxRetriesExceededError) as exc_info
    ):
        await processor.process(session, _VALID_FIELDS)

    assert "max_retries_exceeded" in str(exc_info.value)


@pytest.mark.asyncio
async def test_event_processor_document_not_found_raises() -> None:
    """Document missing from DB → MaxRetriesExceededError."""
    session = MagicMock()
    processor = EventProcessor()

    with patch(
        "src.ingest.event_processor.DocumentRepository.get_by_id",
        new=AsyncMock(return_value=None),
    ), pytest.raises(MaxRetriesExceededError) as exc_info:
        await processor.process(session, _VALID_FIELDS)

    assert "document_not_found" in str(exc_info.value)


# ── IngestWorker DLQ flow ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_moves_event_to_dlq_on_max_retries() -> None:
    """MaxRetriesExceededError from processor → event moved to DLQ, XACK called."""
    worker = IngestWorker()
    worker._running = True

    mock_redis = AsyncMock()
    mock_redis.xack = AsyncMock()
    mock_redis.xadd = AsyncMock()
    mock_redis.xlen = AsyncMock(return_value=1)

    with (
        patch.object(
            worker._processor,
            "process",
            new=AsyncMock(side_effect=MaxRetriesExceededError("test_failure", failure_count=3)),
        ),
        patch("src.ingest.worker.AsyncSessionLocal") as mock_session_cls,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        await worker._process_message(mock_redis, "1234-0", _VALID_FIELDS)

    # XADD to DLQ stream
    mock_redis.xadd.assert_called()
    dlq_call = mock_redis.xadd.call_args_list[0]
    assert dlq_call[0][0] == DLQ_STREAM

    # XACK original message
    mock_redis.xack.assert_called_once_with(STREAM_NAME, "ingest_workers", "1234-0")


@pytest.mark.asyncio
async def test_dlq_event_contains_required_fields() -> None:
    """DLQ event must contain original_event, failure_reason, failure_count, document_id, tenant_id."""
    worker = IngestWorker()

    mock_redis = AsyncMock()
    mock_redis.xadd = AsyncMock()
    mock_redis.xack = AsyncMock()
    mock_redis.xlen = AsyncMock(return_value=1)

    with patch("src.ingest.worker.AsyncSessionLocal") as mock_session_cls:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_cls.return_value = mock_session

        await worker._move_to_dlq(mock_redis, "1234-0", _VALID_FIELDS, "test reason", 3)

    call_args = mock_redis.xadd.call_args[0]
    dlq_data = call_args[1]
    assert "original_event" in dlq_data
    assert "failure_reason" in dlq_data
    assert "failure_count" in dlq_data
    assert "document_id" in dlq_data
    assert "tenant_id" in dlq_data
    assert dlq_data["failure_count"] == "3"
