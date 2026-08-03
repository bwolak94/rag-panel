"""Unit tests for AnalyticsService.

All DB and Redis calls are mocked — no external services required.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.analytics_service import AnalyticsService, _cache_key, _period_to_days

TENANT_ID = uuid.uuid4()


# ── helpers ────────────────────────────────────────────────────────────────


def _make_session() -> MagicMock:
    """Return a mock AsyncSession whose execute() returns an empty result set."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = 0
    result.scalar_one_or_none.return_value = None
    result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


def _make_redis(cached_value: Any = None) -> MagicMock:
    """Return a mock Redis client.

    Args:
        cached_value: If not None, ``redis.get`` will return the JSON-encoded value.
    """
    redis = MagicMock()
    redis.get = AsyncMock(
        return_value=json.dumps(cached_value).encode() if cached_value is not None else None
    )
    redis.setex = AsyncMock(return_value=True)
    return redis


# ── test_get_summary_returns_cached ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_summary_returns_cached() -> None:
    """When Redis has a cached value, SQL must NOT be executed."""
    # Build a minimal serialised AnalyticsSummaryResponse
    cached_payload = {
        "tenant_id": str(TENANT_ID),
        "period_days": 30,
        "active_users": 5,
        "total_queries": 42,
        "total_documents": 10,
        "documents_ready": 8,
        "documents_failed": 1,
        "documents_needs_review": 1,
        "avg_query_latency_ms": None,
        "error_rate_pct": 0.0,
        "top_collections": [],
        "generated_at": datetime.now(UTC).isoformat(),
        "cached": False,
    }

    session = _make_session()
    redis = _make_redis(cached_payload)

    svc = AnalyticsService(session, redis)
    result = await svc.get_summary(TENANT_ID, period_days=30)

    # Cache hit — no DB round-trip
    session.execute.assert_not_called()
    # The cached flag is set to True when served from cache
    assert result.cached is True
    assert result.total_queries == 42
    assert result.tenant_id == TENANT_ID


# ── test_get_summary_sets_cache_ttl_300 ────────────────────────────────────


@pytest.mark.asyncio
async def test_get_summary_sets_cache_ttl_300() -> None:
    """On a cache miss the result is stored in Redis with TTL=300."""
    session = _make_session()
    redis = _make_redis(None)  # cache miss

    svc = AnalyticsService(session, redis)
    await svc.get_summary(TENANT_ID, period_days=7)

    redis.setex.assert_awaited_once()
    call_args = redis.setex.await_args
    # setex(key, ttl, value)
    assert call_args.args[1] == 300  # TTL must be exactly 300 seconds


# ── test_get_document_stats_by_status ──────────────────────────────────────


@pytest.mark.asyncio
async def test_get_document_stats_by_status() -> None:
    """Document counts from DB are exposed correctly in by_status dict."""
    session = MagicMock()
    redis = _make_redis(None)  # cache miss

    # First call: status counts; second call: collection stats; third: ingest status; fourth: avg
    status_result = MagicMock()
    status_result.fetchall.return_value = [("ready", 7), ("failed", 2), ("needs_review", 1)]

    collection_result = MagicMock()
    collection_result.fetchall.return_value = []

    ingest_result = MagicMock()
    ingest_result.fetchall.return_value = [("completed", 5), ("failed", 1)]

    avg_result = MagicMock()
    avg_result.scalar_one.return_value = None

    session.execute = AsyncMock(
        side_effect=[status_result, collection_result, ingest_result, avg_result]
    )

    svc = AnalyticsService(session, redis)
    result = await svc.get_document_stats(TENANT_ID)

    assert result.by_status == {"ready": 7, "failed": 2, "needs_review": 1}
    assert result.ingest_success_rate_pct == pytest.approx(5 / 6 * 100, rel=1e-3)
    assert result.avg_ingest_duration_ms is None


# ── test_analytics_cache_key_is_tenant_scoped ──────────────────────────────


def test_analytics_cache_key_is_tenant_scoped() -> None:
    """Cache key must contain the tenant_id string to prevent cross-tenant bleed."""
    tid_a = uuid.uuid4()
    tid_b = uuid.uuid4()

    key_a = _cache_key(tid_a, "summary", {"period_days": 30})
    key_b = _cache_key(tid_b, "summary", {"period_days": 30})

    assert str(tid_a) in key_a
    assert str(tid_b) in key_b
    # Keys for different tenants must differ
    assert key_a != key_b


# ── test_get_query_series_period_parsing ───────────────────────────────────


def test_get_query_series_period_parsing() -> None:
    """_period_to_days must parse '7d' → 7 and '30d' → 30."""
    assert _period_to_days("7d") == 7
    assert _period_to_days("30d") == 30
    assert _period_to_days("90d") == 90


@pytest.mark.asyncio
async def test_get_query_series_uses_correct_window() -> None:
    """get_query_series for period='7d' must query with a 7-day look-back."""
    session = MagicMock()
    redis = _make_redis(None)

    series_result = MagicMock()
    series_result.fetchall.return_value = []
    session.execute = AsyncMock(return_value=series_result)

    svc = AnalyticsService(session, redis)

    with patch("src.domain.analytics_service.datetime") as mock_dt:
        now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
        mock_dt.now.return_value = now

        result = await svc.get_query_series(TENANT_ID, period="7d", granularity="day")

    assert result.period == "7d"
    assert result.granularity == "day"
    assert result.total_queries == 0

    # The execute was called (one SQL query for the series)
    session.execute.assert_awaited_once()
    # Verify the :since bind param corresponds to 7 days before now
    call_kwargs = session.execute.await_args
    params = call_kwargs.args[1] if call_kwargs.args else call_kwargs.kwargs.get("params", {})
    expected_since = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)
    assert params["since"] == expected_since
