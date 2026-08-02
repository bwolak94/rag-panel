"""AnalyticsService — per-tenant usage analytics with Redis caching.

All SQL queries are filtered by tenant_id; cache keys are tenant-scoped.
Cache failures are silently swallowed — they never break a request.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.schemas.analytics import (
    AnalyticsSummaryResponse,
    CollectionDocumentStats,
    CollectionQueryStats,
    DailyActiveUsers,
    DocumentStatsResponse,
    QueryDataPoint,
    QuerySeriesResponse,
    TopUser,
    UserStatsResponse,
)

logger = structlog.get_logger(__name__)

_CACHE_TTL_SECONDS = 300


def _params_hash(params: dict[str, Any]) -> str:
    """Return an 8-character MD5 hex digest of sorted params."""
    return hashlib.md5(json.dumps(sorted(params.items())).encode()).hexdigest()[:8]


def _cache_key(tenant_id: uuid.UUID, endpoint: str, params: dict[str, Any]) -> str:
    return f"analytics:{tenant_id}:{endpoint}:{_params_hash(params)}"


def _period_to_days(period: str) -> int:
    """Parse a period string such as '7d', '30d', '90d' into an integer number of days."""
    return int(period.rstrip("d"))


class AnalyticsService:
    """Compute per-tenant analytics metrics, backed by Redis cache (TTL 300 s)."""

    def __init__(self, session: AsyncSession, redis: Any) -> None:
        self._session = session
        self._redis = redis

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    async def _get_cached(self, key: str) -> Any | None:
        """Return deserialized value from Redis or None on miss/error."""
        try:
            raw = await self._redis.get(key)
            if raw is not None:
                return json.loads(raw)
        except Exception:
            logger.warning("analytics.cache_get_error", cache_key=key)
        return None

    async def _set_cached(self, key: str, value: Any) -> None:
        """Serialize value and store in Redis with TTL. Failures are silent."""
        try:
            await self._redis.setex(key, _CACHE_TTL_SECONDS, json.dumps(value))
        except Exception:
            logger.warning("analytics.cache_set_error", cache_key=key)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def get_summary(
        self, tenant_id: uuid.UUID, period_days: int = 30
    ) -> AnalyticsSummaryResponse:
        """Return a high-level analytics summary for the tenant.

        Args:
            tenant_id: Tenant to query.
            period_days: Look-back window in days (1–90).

        Returns:
            AnalyticsSummaryResponse populated from DB (or cache).
        """
        params: dict[str, Any] = {"period_days": period_days}
        key = _cache_key(tenant_id, "summary", params)

        cached = await self._get_cached(key)
        if cached is not None:
            cached["cached"] = True
            return AnalyticsSummaryResponse(**cached)

        since = datetime.now(UTC) - timedelta(days=period_days)

        # Active users (distinct user_id in conversations in the period)
        active_users_row = await self._session.execute(
            text(
                "SELECT COUNT(DISTINCT user_id) FROM conversations "
                "WHERE tenant_id = :tid AND created_at >= :since AND is_deleted = false"
            ),
            {"tid": tenant_id, "since": since},
        )
        active_users: int = active_users_row.scalar_one() or 0

        # Total queries = assistant messages in period
        # We join via conversations to enforce tenant_id isolation on messages
        total_queries_row = await self._session.execute(
            text(
                "SELECT COUNT(*) FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.tenant_id = :tid AND m.created_at >= :since AND m.role = 'assistant'"
            ),
            {"tid": tenant_id, "since": since},
        )
        total_queries: int = total_queries_row.scalar_one() or 0

        # Document counts by status
        doc_rows = await self._session.execute(
            text("SELECT status, COUNT(*) FROM documents WHERE tenant_id = :tid GROUP BY status"),
            {"tid": tenant_id},
        )
        doc_by_status: dict[str, int] = {r[0]: int(r[1]) for r in doc_rows.fetchall()}
        total_documents = sum(doc_by_status.values())
        documents_ready = doc_by_status.get("ready", 0)
        documents_failed = doc_by_status.get("failed", 0)
        documents_needs_review = doc_by_status.get("needs_review", 0)

        # Top collections by conversation count in period
        top_rows = await self._session.execute(
            text(
                "SELECT c.pipeline_id, col.id, col.name, COUNT(*) AS q "
                "FROM conversations c "
                "JOIN rag_pipelines rp ON rp.id = c.pipeline_id "
                "JOIN collections col ON col.tenant_id = :tid "
                "WHERE c.tenant_id = :tid AND c.created_at >= :since "
                "  AND c.is_deleted = false "
                "GROUP BY c.pipeline_id, col.id, col.name "
                "ORDER BY q DESC LIMIT 5"
            ),
            {"tid": tenant_id, "since": since},
        )
        # Fallback: top collections by document count if no conversations
        top_rows_fetched = top_rows.fetchall()
        if top_rows_fetched:
            top_collections = [
                CollectionQueryStats(
                    collection_id=r[1],
                    name=r[2],
                    query_count=int(r[3]),
                )
                for r in top_rows_fetched
            ]
        else:
            # Simple fallback: collections with most documents
            fallback_rows = await self._session.execute(
                text(
                    "SELECT id, name, (SELECT COUNT(*) FROM documents d "
                    "WHERE d.collection_id = col.id AND d.tenant_id = :tid) AS dc "
                    "FROM collections col WHERE col.tenant_id = :tid "
                    "ORDER BY dc DESC LIMIT 5"
                ),
                {"tid": tenant_id},
            )
            top_collections = [
                CollectionQueryStats(
                    collection_id=r[0],
                    name=r[1],
                    query_count=int(r[2]),
                )
                for r in fallback_rows.fetchall()
            ]

        result = AnalyticsSummaryResponse(
            tenant_id=tenant_id,
            period_days=period_days,
            active_users=active_users,
            total_queries=total_queries,
            total_documents=total_documents,
            documents_ready=documents_ready,
            documents_failed=documents_failed,
            documents_needs_review=documents_needs_review,
            avg_query_latency_ms=None,
            error_rate_pct=0.0,
            top_collections=top_collections,
            generated_at=datetime.now(UTC),
            cached=False,
        )

        await self._set_cached(key, result.model_dump(mode="json"))
        return result

    async def get_query_series(
        self,
        tenant_id: uuid.UUID,
        period: str = "7d",
        granularity: str = "day",
    ) -> QuerySeriesResponse:
        """Return time-series query counts grouped by day/week.

        Args:
            tenant_id: Tenant to query.
            period: Look-back window string such as '7d', '30d', '90d'.
            granularity: Grouping granularity — 'day' or 'week'.

        Returns:
            QuerySeriesResponse with one data point per granularity bucket.
        """
        period_days = _period_to_days(period)
        params: dict[str, Any] = {"period": period, "granularity": granularity}
        key = _cache_key(tenant_id, "queries", params)

        cached = await self._get_cached(key)
        if cached is not None:
            return QuerySeriesResponse(**cached)

        since = datetime.now(UTC) - timedelta(days=period_days)

        date_trunc = "week" if granularity == "week" else "day"

        rows = await self._session.execute(
            text(
                f"SELECT DATE_TRUNC('{date_trunc}', m.created_at)::date AS bucket, "
                "COUNT(*) AS query_count, "
                "COUNT(DISTINCT c.user_id) AS unique_users, "
                "0 AS error_count "
                "FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.tenant_id = :tid AND m.created_at >= :since AND m.role = 'assistant' "
                f"GROUP BY DATE_TRUNC('{date_trunc}', m.created_at)::date "
                "ORDER BY bucket ASC"
            ),
            {"tid": tenant_id, "since": since},
        )
        fetched = rows.fetchall()

        series = [
            QueryDataPoint(
                date=str(r[0]),
                query_count=int(r[1]),
                unique_users=int(r[2]),
                error_count=int(r[3]),
            )
            for r in fetched
        ]
        total_queries = sum(dp.query_count for dp in series)

        result = QuerySeriesResponse(
            series=series,
            total_queries=total_queries,
            period=period,
            granularity=granularity,
        )

        await self._set_cached(key, result.model_dump(mode="json"))
        return result

    async def get_document_stats(self, tenant_id: uuid.UUID) -> DocumentStatsResponse:
        """Return document statistics: by status, by collection, ingest success rate.

        Args:
            tenant_id: Tenant to query.

        Returns:
            DocumentStatsResponse with counts and ingest success rate.
        """
        params: dict[str, Any] = {}
        key = _cache_key(tenant_id, "documents", params)

        cached = await self._get_cached(key)
        if cached is not None:
            return DocumentStatsResponse(**cached)

        # By status
        status_rows = await self._session.execute(
            text("SELECT status, COUNT(*) FROM documents WHERE tenant_id = :tid GROUP BY status"),
            {"tid": tenant_id},
        )
        by_status: dict[str, int] = {r[0]: int(r[1]) for r in status_rows.fetchall()}

        # By collection (JOIN to get collection name)
        col_rows = await self._session.execute(
            text(
                "SELECT col.id, col.name, COUNT(d.id) AS doc_count, "
                "COALESCE(SUM(d.size_bytes), 0) AS total_bytes "
                "FROM collections col "
                "LEFT JOIN documents d ON d.collection_id = col.id AND d.tenant_id = :tid "
                "WHERE col.tenant_id = :tid "
                "GROUP BY col.id, col.name "
                "ORDER BY doc_count DESC"
            ),
            {"tid": tenant_id},
        )
        by_collection = [
            CollectionDocumentStats(
                collection_id=r[0],
                name=r[1],
                document_count=int(r[2]),
                total_size_bytes=int(r[3]),
            )
            for r in col_rows.fetchall()
        ]

        # Ingest success rate from ingestion_jobs
        ingest_rows = await self._session.execute(
            text(
                "SELECT status, COUNT(*) FROM ingestion_jobs WHERE tenant_id = :tid GROUP BY status"
            ),
            {"tid": tenant_id},
        )
        ingest_by_status: dict[str, int] = {r[0]: int(r[1]) for r in ingest_rows.fetchall()}
        total_jobs = sum(ingest_by_status.values())
        completed_jobs = ingest_by_status.get("completed", 0)
        ingest_success_rate = (completed_jobs / total_jobs * 100.0) if total_jobs > 0 else 0.0

        # Average ingest duration (completed_at - started_at in ms)
        avg_row = await self._session.execute(
            text(
                "SELECT AVG(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000) "
                "FROM ingestion_jobs "
                "WHERE tenant_id = :tid AND status = 'completed' "
                "  AND started_at IS NOT NULL AND completed_at IS NOT NULL"
            ),
            {"tid": tenant_id},
        )
        avg_ingest_ms_raw = avg_row.scalar_one()
        avg_ingest_ms: float | None = float(avg_ingest_ms_raw) if avg_ingest_ms_raw else None

        result = DocumentStatsResponse(
            by_status=by_status,
            by_collection=by_collection,
            ingest_success_rate_pct=round(ingest_success_rate, 2),
            avg_ingest_duration_ms=avg_ingest_ms,
        )

        await self._set_cached(key, result.model_dump(mode="json"))
        return result

    async def get_user_stats(
        self, tenant_id: uuid.UUID, period_days: int = 30
    ) -> UserStatsResponse:
        """Return user activity statistics: MAU, DAU (7 days), top users by query count.

        Args:
            tenant_id: Tenant to query.
            period_days: Look-back window for MAU calculation (7–90).

        Returns:
            UserStatsResponse.
        """
        params: dict[str, Any] = {"period_days": period_days}
        key = _cache_key(tenant_id, "users", params)

        cached = await self._get_cached(key)
        if cached is not None:
            return UserStatsResponse(**cached)

        since_30d = datetime.now(UTC) - timedelta(days=period_days)
        since_7d = datetime.now(UTC) - timedelta(days=7)

        # MAU — distinct users with a conversation in the period
        mau_row = await self._session.execute(
            text(
                "SELECT COUNT(DISTINCT user_id) FROM conversations "
                "WHERE tenant_id = :tid AND created_at >= :since AND is_deleted = false"
            ),
            {"tid": tenant_id, "since": since_30d},
        )
        mau_30d: int = mau_row.scalar_one() or 0

        # DAU — distinct users per day over last 7 days
        dau_rows = await self._session.execute(
            text(
                "SELECT DATE(created_at) AS day, COUNT(DISTINCT user_id) AS dau "
                "FROM conversations "
                "WHERE tenant_id = :tid AND created_at >= :since AND is_deleted = false "
                "GROUP BY DATE(created_at) ORDER BY day ASC"
            ),
            {"tid": tenant_id, "since": since_7d},
        )
        dau_7d = [
            DailyActiveUsers(date=str(r[0]), active_users=int(r[1])) for r in dau_rows.fetchall()
        ]

        # Top users by number of assistant messages (queries) in period
        top_rows = await self._session.execute(
            text(
                "SELECT c.user_id, u.display_name, COUNT(m.id) AS q "
                "FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "JOIN users u ON u.id = c.user_id "
                "WHERE c.tenant_id = :tid AND m.created_at >= :since AND m.role = 'assistant' "
                "GROUP BY c.user_id, u.display_name "
                "ORDER BY q DESC LIMIT 10"
            ),
            {"tid": tenant_id, "since": since_30d},
        )
        top_users = [
            TopUser(
                user_id=r[0],
                display_name=r[1] or "",
                query_count=int(r[2]),
            )
            for r in top_rows.fetchall()
        ]

        result = UserStatsResponse(
            mau_30d=mau_30d,
            dau_7d=dau_7d,
            top_users_by_queries=top_users,
        )

        await self._set_cached(key, result.model_dump(mode="json"))
        return result
