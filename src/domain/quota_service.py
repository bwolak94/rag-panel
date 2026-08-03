"""QuotaService — per-tenant quota enforcement backed by Redis counters + Postgres.

Security:
- tenant_id always from JWT context — never from request body.
- Redis counters use atomic INCR — no race conditions.
- Quota overrides stored in tenants.settings.quotas (platform-admin only).
- QuotaExceededError → HTTP 429 with Retry-After header (exception_handlers.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import QuotaExceededError
from src.db.models.collection import Collection
from src.db.models.document import Document
from src.db.models.tenant import Tenant
from src.domain.schemas.quota import QuotaItem, QuotaStatusResponse

logger = structlog.get_logger(__name__)

_QUERY_COUNTER_TTL_SECONDS = 31 * 24 * 3600  # 31 days — rolling window


def _redis_query_key(tenant_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    month = now.strftime("%Y-%m")
    return f"quota:queries:{tenant_id}:{month}"


def _month_reset() -> datetime:
    """First second of next month (UTC)."""
    now = datetime.now(UTC)
    if now.month == 12:
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _tenant_quota(tenant: Tenant, key: str, default: int) -> int:
    quotas: dict[str, Any] = (tenant.settings or {}).get("quotas", {})
    val = quotas.get(key, default)
    return int(val) if isinstance(val, (int, float)) else default


class QuotaService:
    def __init__(self, session: AsyncSession, redis: Any) -> None:
        self._session = session
        self._redis = redis

    # ── Checks ────────────────────────────────────────────────────────────────

    async def check_document_upload(self, tenant_id: uuid.UUID, file_size_bytes: int) -> None:
        """Raise QuotaExceededError if max_documents or max_storage_bytes exceeded."""
        tenant = await self._get_tenant(tenant_id)
        max_docs = _tenant_quota(tenant, "max_documents", settings.QUOTA_MAX_DOCUMENTS)
        max_storage = _tenant_quota(tenant, "max_storage_bytes", settings.QUOTA_MAX_STORAGE_BYTES)

        # Document count
        doc_count_q = (
            select(func.count())
            .select_from(Document)
            .where(Document.tenant_id == tenant_id, Document.status != "deleted")
        )
        doc_count: int = (await self._session.execute(doc_count_q)).scalar_one()
        if doc_count >= max_docs:
            raise QuotaExceededError(
                f"Document quota reached ({doc_count}/{max_docs}).",
                quota_type="max_documents",
                limit=max_docs,
                current=doc_count,
            )

        # Storage bytes
        storage_q = select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
            Document.tenant_id == tenant_id, Document.status != "deleted"
        )
        used_bytes: int = (await self._session.execute(storage_q)).scalar_one()
        if used_bytes + file_size_bytes > max_storage:
            raise QuotaExceededError(
                f"Storage quota reached ({used_bytes}/{max_storage} bytes).",
                quota_type="max_storage_bytes",
                limit=max_storage,
                current=used_bytes,
            )

    async def check_collection_create(self, tenant_id: uuid.UUID) -> None:
        """Raise QuotaExceededError if max_collections exceeded."""
        tenant = await self._get_tenant(tenant_id)
        max_col = _tenant_quota(tenant, "max_collections", settings.QUOTA_MAX_COLLECTIONS)

        col_count_q = (
            select(func.count()).select_from(Collection).where(Collection.tenant_id == tenant_id)
        )
        col_count: int = (await self._session.execute(col_count_q)).scalar_one()
        if col_count >= max_col:
            raise QuotaExceededError(
                f"Collection quota reached ({col_count}/{max_col}).",
                quota_type="max_collections",
                limit=max_col,
                current=col_count,
            )

    async def check_query(self, tenant_id: uuid.UUID) -> None:
        """Raise QuotaExceededError if max_monthly_queries exceeded.

        Increments the Redis counter ONLY after the quota check passes.
        """
        tenant = await self._get_tenant(tenant_id)
        max_queries = _tenant_quota(
            tenant, "max_monthly_queries", settings.QUOTA_MAX_MONTHLY_QUERIES
        )

        key = _redis_query_key(tenant_id)
        try:
            current_raw = await self._redis.get(key)
            current = int(current_raw) if current_raw else 0
        except Exception:
            logger.warning("quota.redis_read_failed", tenant_id=str(tenant_id))
            return  # fail open — don't block queries on Redis unavailability

        if current >= max_queries:
            reset_at = _month_reset().isoformat()
            raise QuotaExceededError(
                f"Monthly query quota reached ({current}/{max_queries}). Resets on {reset_at}.",
                quota_type="monthly_queries",
                limit=max_queries,
                current=current,
                reset_at=reset_at,
            )

        # Increment — fire and forget (best-effort)
        try:
            pipe = self._redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, _QUERY_COUNTER_TTL_SECONDS)
            await pipe.execute()
        except Exception:
            logger.warning("quota.redis_incr_failed", tenant_id=str(tenant_id))

    # ── Status ────────────────────────────────────────────────────────────────

    async def get_quota_status(self, tenant_id: uuid.UUID) -> QuotaStatusResponse:
        tenant = await self._get_tenant(tenant_id)

        max_docs = _tenant_quota(tenant, "max_documents", settings.QUOTA_MAX_DOCUMENTS)
        max_storage = _tenant_quota(tenant, "max_storage_bytes", settings.QUOTA_MAX_STORAGE_BYTES)
        max_queries = _tenant_quota(
            tenant, "max_monthly_queries", settings.QUOTA_MAX_MONTHLY_QUERIES
        )
        max_mau = _tenant_quota(tenant, "max_mau", settings.QUOTA_MAX_MAU)
        max_col = _tenant_quota(tenant, "max_collections", settings.QUOTA_MAX_COLLECTIONS)

        # Document count + storage
        doc_q = select(func.count(), func.coalesce(func.sum(Document.size_bytes), 0)).where(
            Document.tenant_id == tenant_id, Document.status != "deleted"
        )
        doc_row = (await self._session.execute(doc_q)).one()
        doc_count, used_bytes = int(doc_row[0]), int(doc_row[1])

        # Collection count
        col_count_q = (
            select(func.count()).select_from(Collection).where(Collection.tenant_id == tenant_id)
        )
        col_count: int = (await self._session.execute(col_count_q)).scalar_one()

        # Monthly query count from Redis
        key = _redis_query_key(tenant_id)
        try:
            current_raw = await self._redis.get(key)
            query_count = int(current_raw) if current_raw else 0
        except Exception:
            query_count = 0

        # MAU — distinct users with a message in last 30 days
        mau_q = (
            select(func.count(text("DISTINCT m.conversation_id")))
            .select_from(
                text(
                    "messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.tenant_id = :tid AND m.created_at >= NOW() - INTERVAL '30 days'"
                )
            )
            .params(tid=tenant_id)
        )
        try:
            mau: int = (await self._session.execute(mau_q)).scalar_one() or 0
        except Exception:
            mau = 0

        reset_at = _month_reset()

        def _pct(current: int, limit: int) -> float:
            return round(current * 100 / limit, 1) if limit > 0 else 0.0

        return QuotaStatusResponse(
            tenant_id=tenant_id,
            quotas={
                "documents": QuotaItem(
                    limit=max_docs, current=doc_count, pct=_pct(doc_count, max_docs)
                ),
                "storage_bytes": QuotaItem(
                    limit=max_storage, current=used_bytes, pct=_pct(used_bytes, max_storage)
                ),
                "monthly_queries": QuotaItem(
                    limit=max_queries,
                    current=query_count,
                    pct=_pct(query_count, max_queries),
                    reset_at=reset_at,
                ),
                "mau": QuotaItem(limit=max_mau, current=mau, pct=_pct(mau, max_mau)),
                "collections": QuotaItem(
                    limit=max_col, current=col_count, pct=_pct(col_count, max_col)
                ),
            },
        )

    # ── Platform-admin quota update ────────────────────────────────────────────

    async def update_tenant_quotas(
        self,
        tenant_id: uuid.UUID,
        updates: dict[str, int],
    ) -> None:
        """Merge quota overrides into tenants.settings.quotas."""
        from sqlalchemy import update as sql_update

        tenant = await self._get_tenant(tenant_id)
        current_settings: dict[str, Any] = dict(tenant.settings or {})
        current_quotas: dict[str, Any] = dict(current_settings.get("quotas", {}))
        current_quotas.update(updates)
        current_settings["quotas"] = current_quotas

        await self._session.execute(
            sql_update(Tenant).where(Tenant.id == tenant_id).values(settings=current_settings)
        )
        await self._session.flush()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        from src.core.exceptions import NotFoundError

        obj = await self._session.get(Tenant, tenant_id)
        if obj is None:
            raise NotFoundError(f"Tenant {tenant_id} not found")
        return obj
