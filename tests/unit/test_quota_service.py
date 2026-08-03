"""Unit tests for QuotaService — quota enforcement logic."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import QuotaExceededError

# ── check_query ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_query_raises_when_limit_reached() -> None:
    """check_query raises QuotaExceededError when monthly counter >= limit."""
    session = MagicMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"10000")

    tenant = MagicMock()
    tenant.settings = {}  # use defaults from settings
    session.get = AsyncMock(return_value=tenant)

    from src.domain.quota_service import QuotaService

    svc = QuotaService(session, redis)

    with patch("src.domain.quota_service.settings") as mock_settings:
        mock_settings.QUOTA_MAX_MONTHLY_QUERIES = 10000

        with pytest.raises(QuotaExceededError) as exc_info:
            await svc.check_query(uuid.uuid4())

    assert exc_info.value.quota_type == "monthly_queries"
    assert exc_info.value.limit == 10000
    assert exc_info.value.current == 10000


@pytest.mark.asyncio
async def test_check_query_passes_and_increments() -> None:
    """check_query succeeds and increments the Redis counter when under limit."""
    session = MagicMock()
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"100")
    pipe_mock = AsyncMock()
    pipe_mock.incr = MagicMock()
    pipe_mock.expire = MagicMock()
    pipe_mock.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=pipe_mock)

    tenant = MagicMock()
    tenant.settings = {}
    session.get = AsyncMock(return_value=tenant)

    from src.domain.quota_service import QuotaService

    svc = QuotaService(session, redis)

    with patch("src.domain.quota_service.settings") as mock_settings:
        mock_settings.QUOTA_MAX_MONTHLY_QUERIES = 10000
        await svc.check_query(uuid.uuid4())

    pipe_mock.incr.assert_called_once()


@pytest.mark.asyncio
async def test_check_query_fails_open_on_redis_error() -> None:
    """check_query does NOT block when Redis is unavailable (fail open)."""
    session = MagicMock()
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))

    tenant = MagicMock()
    tenant.settings = {}
    session.get = AsyncMock(return_value=tenant)

    from src.domain.quota_service import QuotaService

    svc = QuotaService(session, redis)

    with patch("src.domain.quota_service.settings") as mock_settings:
        mock_settings.QUOTA_MAX_MONTHLY_QUERIES = 10000
        # Should NOT raise — fail open
        await svc.check_query(uuid.uuid4())


# ── check_document_upload ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_document_upload_raises_on_doc_count_limit() -> None:
    """check_document_upload raises when document count >= limit."""
    session = MagicMock()
    redis = AsyncMock()

    tenant = MagicMock()
    tenant.settings = {}
    session.get = AsyncMock(return_value=tenant)

    # Mock execute: first call = document count (500), second = storage bytes (0)
    count_result = MagicMock()
    count_result.scalar_one = MagicMock(return_value=500)
    storage_result = MagicMock()
    storage_result.scalar_one = MagicMock(return_value=0)
    session.execute = AsyncMock(side_effect=[count_result, storage_result])

    from src.domain.quota_service import QuotaService

    svc = QuotaService(session, redis)

    with patch("src.domain.quota_service.settings") as mock_settings:
        mock_settings.QUOTA_MAX_DOCUMENTS = 500
        mock_settings.QUOTA_MAX_STORAGE_BYTES = 5 * 1024 * 1024 * 1024

        with pytest.raises(QuotaExceededError) as exc_info:
            await svc.check_document_upload(uuid.uuid4(), 1024)

    assert exc_info.value.quota_type == "max_documents"


# ── QuotaExceededError ─────────────────────────────────────────────────────


def test_quota_exceeded_error_attributes() -> None:
    err = QuotaExceededError(
        "Test quota",
        quota_type="monthly_queries",
        limit=1000,
        current=1001,
        reset_at="2026-09-01T00:00:00Z",
    )
    assert err.quota_type == "monthly_queries"
    assert err.limit == 1000
    assert err.current == 1001
    assert err.reset_at == "2026-09-01T00:00:00Z"
    assert str(err) == "Test quota"
