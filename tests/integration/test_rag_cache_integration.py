"""Integration tests for RAGCache against a real Redis instance.

Uses testcontainers to spin up Redis. Tests are skipped automatically if
Docker is unavailable or testcontainers is not installed.

Mark: @pytest.mark.integration
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

try:
    from testcontainers.core.container import DockerContainer  # type: ignore[import-untyped]
    from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-untyped]
except ImportError:
    pytest.skip("testcontainers not installed", allow_module_level=True)

try:
    import redis.asyncio as aioredis  # type: ignore[import-untyped]
except ImportError:
    pytest.skip("redis[asyncio] not installed", allow_module_level=True)

# Guard: skip if the Docker daemon is not reachable at module import time.
try:
    import docker  # type: ignore[import-untyped]

    docker.from_env().ping()
except Exception:
    pytest.skip("Docker daemon not available", allow_module_level=True)

from src.core.cache import RAGCache  # noqa: E402

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
PIPELINE_A = uuid.uuid4()
COLLECTION_A = uuid.uuid4()
COLLECTION_B = uuid.uuid4()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def redis_container() -> Any:
    """Start a Redis container for the test module."""
    container = DockerContainer("redis:7-alpine")
    container.with_exposed_ports(6379)
    container.start()
    wait_for_logs(container, "Ready to accept connections", timeout=30)
    yield container
    container.stop()


@pytest.fixture(scope="module")
def redis_url(redis_container: Any) -> str:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}/0"


@pytest.fixture
async def redis_client(redis_url: str) -> Any:
    """Return a fresh async Redis client per test; flush all keys before/after."""
    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushall()
    yield client
    await client.flushall()
    await client.aclose()


@pytest.fixture
def cache(redis_client: Any) -> RAGCache:
    """RAGCache backed by the real Redis container."""
    return RAGCache(redis=redis_client)


# ── Tests: Retrieval cache ─────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_cache_miss_returns_none(cache: RAGCache) -> None:
    """Cold cache must return None, not raise."""
    result = await cache.get_retrieval(TENANT_A, [COLLECTION_A], "what is a diagnosis?")
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_cache_set_then_get_roundtrip(cache: RAGCache) -> None:
    """Stored retrieval chunks are returned verbatim on cache hit."""
    query = "patient blood pressure medication"
    chunks: list[dict[str, Any]] = [
        {"text": "Chunk one", "score": 0.9, "doc_id": str(uuid.uuid4())},
        {"text": "Chunk two", "score": 0.8, "doc_id": str(uuid.uuid4())},
    ]

    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query, chunks)
    result = await cache.get_retrieval(TENANT_A, [COLLECTION_A], query)

    assert result is not None
    assert len(result) == 2
    assert result[0]["text"] == "Chunk one"
    assert result[1]["text"] == "Chunk two"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_cache_ttl_expiry(cache: RAGCache) -> None:
    """Cache entry disappears after the TTL has elapsed (1-second TTL)."""
    query = "short-lived query"
    chunks: list[dict[str, Any]] = [{"text": "temporary"}]

    # Store with 1-second TTL
    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query, chunks, ttl=1)

    # Confirm it is there immediately
    hit = await cache.get_retrieval(TENANT_A, [COLLECTION_A], query)
    assert hit is not None

    # Wait for the key to expire
    await asyncio.sleep(1.2)

    miss = await cache.get_retrieval(TENANT_A, [COLLECTION_A], query)
    assert miss is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retrieval_cache_tenant_isolation(cache: RAGCache) -> None:
    """Entries for Tenant A must not be visible to Tenant B."""
    query = "shared query text"
    chunks_a: list[dict[str, Any]] = [{"text": "Only Tenant A sees this"}]

    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query, chunks_a)

    # Tenant B with same query → cache miss
    result_b = await cache.get_retrieval(TENANT_B, [COLLECTION_A], query)
    assert result_b is None, "Tenant B must not receive Tenant A's cached results"

    # Tenant A still gets a hit
    result_a = await cache.get_retrieval(TENANT_A, [COLLECTION_A], query)
    assert result_a is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_collection_deletes_retrieval_keys(cache: RAGCache) -> None:
    """invalidate_collection() removes all retrieval cache keys for that collection."""
    query_1 = "first query"
    query_2 = "second query"

    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query_1, [{"text": "q1"}])
    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query_2, [{"text": "q2"}])

    # Confirm both are cached
    assert await cache.get_retrieval(TENANT_A, [COLLECTION_A], query_1) is not None
    assert await cache.get_retrieval(TENANT_A, [COLLECTION_A], query_2) is not None

    deleted = await cache.invalidate_collection(TENANT_A, COLLECTION_A)
    assert deleted > 0  # at least the two retrieval keys + the invalidation set key

    # Both entries must now be gone
    assert await cache.get_retrieval(TENANT_A, [COLLECTION_A], query_1) is None
    assert await cache.get_retrieval(TENANT_A, [COLLECTION_A], query_2) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_collection_does_not_affect_other_collection(
    cache: RAGCache,
) -> None:
    """Invalidating collection A must leave collection B's cache entries intact."""
    query = "cross-collection query"

    await cache.set_retrieval(TENANT_A, [COLLECTION_A], query, [{"text": "from A"}])
    await cache.set_retrieval(TENANT_A, [COLLECTION_B], query, [{"text": "from B"}])

    await cache.invalidate_collection(TENANT_A, COLLECTION_A)

    # Collection B entry must survive
    result_b = await cache.get_retrieval(TENANT_A, [COLLECTION_B], query)
    assert result_b is not None
    assert result_b[0]["text"] == "from B"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalidate_collection_empty_returns_zero(cache: RAGCache) -> None:
    """Calling invalidate_collection when nothing is cached returns 0, not an error."""
    deleted = await cache.invalidate_collection(TENANT_A, uuid.uuid4())
    assert deleted == 0


# ── Tests: Response cache ──────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_cache_miss_returns_none(cache: RAGCache) -> None:
    """Cold response cache returns None."""
    result = await cache.get_response(TENANT_A, PIPELINE_A, "any query")
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_cache_set_then_get_roundtrip(cache: RAGCache) -> None:
    """Stored LLM response is returned verbatim on hit."""
    query = "What is hypertension?"
    answer = "Hypertension is elevated blood pressure."
    citations: list[dict[str, Any]] = [{"doc_id": str(uuid.uuid4()), "chunk": "..."}]

    await cache.set_response(TENANT_A, PIPELINE_A, query, answer, citations)
    result = await cache.get_response(TENANT_A, PIPELINE_A, query)

    assert result is not None
    assert result["answer"] == answer
    assert len(result["citations"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_cache_ttl_expiry(cache: RAGCache) -> None:
    """Response cache entry expires after the TTL."""
    query = "expiring response"
    await cache.set_response(TENANT_A, PIPELINE_A, query, "answer", [], ttl=1)

    assert await cache.get_response(TENANT_A, PIPELINE_A, query) is not None

    await asyncio.sleep(1.2)

    assert await cache.get_response(TENANT_A, PIPELINE_A, query) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_cache_tenant_isolation(cache: RAGCache) -> None:
    """Tenant B cannot see Tenant A's cached response."""
    query = "isolation test"
    await cache.set_response(TENANT_A, PIPELINE_A, query, "secret answer", [])

    result_b = await cache.get_response(TENANT_B, PIPELINE_A, query)
    assert result_b is None, "Tenant B must not receive Tenant A's cached response"
