"""Async Redis client singleton."""

from __future__ import annotations

import redis.asyncio as aioredis

from src.core.config import settings

_redis: aioredis.Redis[bytes] | None = None


async def get_redis_client() -> aioredis.Redis[bytes]:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            str(settings.REDIS_URL),
            socket_connect_timeout=2,
            socket_timeout=6,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis
