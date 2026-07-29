"""Async Redis client singleton."""

from __future__ import annotations

import redis.asyncio as aioredis

from src.core.config import settings

_redis: aioredis.Redis[bytes] | None = None


def get_redis_client() -> aioredis.Redis[bytes]:
    """Return the global async Redis singleton (created lazily on first call).

    The connection pool is established lazily on the first command — no event loop
    is needed at call time.  Safe to call from both sync and async contexts.
    """
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            str(settings.REDIS_URL),
            socket_connect_timeout=2,
            socket_timeout=6,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis
