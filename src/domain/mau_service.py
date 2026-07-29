"""MauService — Monthly Active User counter with Redis caching."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import Conversation

logger = structlog.get_logger(__name__)

_MAU_CACHE_TTL_SECONDS = 3600
_MAU_WARNING_THRESHOLD = 40
_MAU_ACTION_REQUIRED_THRESHOLD = 50


class MauService:
    """Counts distinct active users per tenant per calendar month."""

    def __init__(self, session: AsyncSession, redis: Redis) -> None:  # type: ignore[type-arg]
        self._session = session
        self._redis = redis

    async def get_mau_count(self, tenant_id: uuid.UUID) -> int:
        """Return the number of distinct users who started a conversation this month.

        Result is cached in Redis for 1 hour.

        Args:
            tenant_id: UUID of the tenant to count.

        Returns:
            Integer count of distinct user_id values in the current calendar month.
        """
        today = date.today()
        month_key = today.strftime("%Y-%m")
        cache_key = f"mau:tenant:{tenant_id}:{month_key}"

        cached = await self._redis.get(cache_key)
        if cached is not None:
            return int(cached)

        # Compute date range for the current month (UTC)
        first_day = datetime(today.year, today.month, 1, tzinfo=UTC)
        next_month_first = datetime(
            today.year + (today.month // 12),
            (today.month % 12) + 1,
            1,
            tzinfo=UTC,
        )

        q = select(func.count(func.distinct(Conversation.user_id))).where(
            Conversation.tenant_id == tenant_id,
            Conversation.created_at >= first_day,
            Conversation.created_at < next_month_first,
            Conversation.is_deleted.is_(False),
        )
        count: int = (await self._session.execute(q)).scalar_one()

        await self._redis.set(cache_key, str(count), ex=_MAU_CACHE_TTL_SECONDS)

        logger.info("mau.count_computed", tenant_id=str(tenant_id), count=count, month=month_key)
        return count

    async def get_mau_status(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        """Return MAU metrics dict for inclusion in TosStatusResponse.

        Returns:
            Dict with keys:
                count (int): number of MAU this month.
                warning (bool): True when count >= 40.
                action_required (bool): True when count >= 50.
        """
        count = await self.get_mau_count(tenant_id)
        return {
            "count": count,
            "warning": count >= _MAU_WARNING_THRESHOLD,
            "action_required": count >= _MAU_ACTION_REQUIRED_THRESHOLD,
        }
