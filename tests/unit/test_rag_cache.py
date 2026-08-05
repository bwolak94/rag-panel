"""Unit tests for RAGCache (src/core/cache.py).

Tests cover:
- get_response() returns None on cache miss; cached value on hit
- set_response() stores with correct TTL
- get_retrieval() / set_retrieval() round-trip
- invalidate_collection() deletes correct keys
- Cache keys are tenant-scoped (different tenant → different key)
- Redis errors are swallowed (fail-open)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.cache import RAGCache, _response_key, _retrieval_key


async def _empty_async_iter(*_args: object, **_kwargs: object) -> object:
    """Async generator yielding nothing — simulates empty sscan_iter."""
    return
    yield  # type: ignore[misc]  # unreachable, but required to make this a generator


def _sscan_iter_returning(*items: bytes) -> object:
    """Return an async generator that yields the given items."""

    async def _gen(*_args: object, **_kwargs: object) -> object:
        for item in items:
            yield item

    return _gen


def _make_redis(get_return: bytes | None = None) -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=get_return)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock(return_value=1)
    redis.sscan_iter = _empty_async_iter  # default: no members

    pipeline_mock = MagicMock()
    pipeline_mock.__aenter__ = AsyncMock(return_value=pipeline_mock)
    pipeline_mock.__aexit__ = AsyncMock(return_value=False)
    pipeline_mock.setex = MagicMock()
    pipeline_mock.sadd = MagicMock()
    pipeline_mock.expire = MagicMock()
    pipeline_mock.execute = AsyncMock(return_value=[True, 1, True])
    redis.pipeline = MagicMock(return_value=pipeline_mock)

    return redis


TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()


# ── Key isolation ─────────────────────────────────────────────────────────────


def test_response_keys_are_tenant_scoped() -> None:
    """Different tenant_id → different response cache key."""
    key_a = _response_key(TENANT_A, PIPELINE_ID, "query")
    key_b = _response_key(TENANT_B, PIPELINE_ID, "query")
    assert key_a != key_b


def test_retrieval_keys_are_tenant_scoped() -> None:
    """Different tenant_id → different retrieval cache key."""
    key_a = _retrieval_key(TENANT_A, [COLLECTION_ID], "query")
    key_b = _retrieval_key(TENANT_B, [COLLECTION_ID], "query")
    assert key_a != key_b


def test_retrieval_key_collection_order_independent() -> None:
    """Collection IDs are sorted → same key regardless of order."""
    cid1, cid2 = uuid.uuid4(), uuid.uuid4()
    key_ab = _retrieval_key(TENANT_A, [cid1, cid2], "q")
    key_ba = _retrieval_key(TENANT_A, [cid2, cid1], "q")
    assert key_ab == key_ba


# ── Response cache ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_response_returns_none_on_miss() -> None:
    redis = _make_redis(get_return=None)
    cache = RAGCache(redis)
    result = await cache.get_response(TENANT_A, PIPELINE_ID, "question?")
    assert result is None


@pytest.mark.asyncio
async def test_get_response_returns_value_on_hit() -> None:
    payload = json.dumps({"answer": "Paris", "citations": []}).encode()
    redis = _make_redis(get_return=payload)
    cache = RAGCache(redis)
    result = await cache.get_response(TENANT_A, PIPELINE_ID, "capital of France?")
    assert result is not None
    assert result["answer"] == "Paris"


@pytest.mark.asyncio
async def test_set_response_uses_correct_ttl() -> None:
    redis = _make_redis()
    cache = RAGCache(redis, response_ttl=3600)
    await cache.set_response(TENANT_A, PIPELINE_ID, "q", "answer", [], ttl=7200)
    redis.setex.assert_awaited_once()
    args = redis.setex.call_args[0]
    assert args[1] == 7200  # custom TTL used


@pytest.mark.asyncio
async def test_set_response_uses_default_ttl_when_none() -> None:
    redis = _make_redis()
    cache = RAGCache(redis, response_ttl=3600)
    await cache.set_response(TENANT_A, PIPELINE_ID, "q", "answer", [])
    args = redis.setex.call_args[0]
    assert args[1] == 3600  # default TTL


@pytest.mark.asyncio
async def test_get_response_swallows_redis_error() -> None:
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    cache = RAGCache(redis)
    result = await cache.get_response(TENANT_A, PIPELINE_ID, "q")
    assert result is None  # fail-open


# ── Retrieval cache ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_retrieval_returns_none_on_miss() -> None:
    redis = _make_redis(get_return=None)
    cache = RAGCache(redis)
    result = await cache.get_retrieval(TENANT_A, [COLLECTION_ID], "query")
    assert result is None


@pytest.mark.asyncio
async def test_get_retrieval_returns_chunks_on_hit() -> None:
    chunks = [{"point_id": "abc", "score": 0.9}]
    payload = json.dumps(chunks).encode()
    redis = _make_redis(get_return=payload)
    cache = RAGCache(redis)
    result = await cache.get_retrieval(TENANT_A, [COLLECTION_ID], "query")
    assert result == chunks


@pytest.mark.asyncio
async def test_set_retrieval_registers_invalidation_keys() -> None:
    redis = _make_redis()
    cache = RAGCache(redis)
    chunks = [{"point_id": "xyz", "score": 0.8}]
    await cache.set_retrieval(TENANT_A, [COLLECTION_ID], "query", chunks)
    # Pipeline executed → pipeline.sadd called to register in invalidation set
    pipeline = redis.pipeline.return_value.__aenter__.return_value
    pipeline.sadd.assert_called()


# ── Cache invalidation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_collection_returns_zero_when_no_keys() -> None:
    redis = _make_redis()
    redis.sscan_iter = _empty_async_iter  # no members
    cache = RAGCache(redis)
    deleted = await cache.invalidate_collection(TENANT_A, COLLECTION_ID)
    assert deleted == 0
    redis.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalidate_collection_deletes_keys() -> None:
    key1 = b"rag:ret:tenant:abc"
    key2 = b"rag:ret:tenant:def"
    redis = _make_redis()
    redis.sscan_iter = _sscan_iter_returning(key1, key2)
    redis.delete = AsyncMock(return_value=3)
    cache = RAGCache(redis)
    deleted = await cache.invalidate_collection(TENANT_A, COLLECTION_ID)
    assert deleted == 3
    redis.delete.assert_awaited_once()
