"""RAG response and retrieval cache backed by Redis.

Two cache levels:
  1. Retrieval cache  — stores Qdrant search results for (tenant, collections, query).
     TTL default: 1800s. Invalidated when a new document is indexed in the collection.
  2. Response cache   — stores final LLM-generated answers for (tenant, pipeline, query).
     TTL default: 3600s. Opt-in per pipeline (cache_responses=False by default — medical
     answers should be fresh).

Cache key design:
  retrieval: rag:ret:{tenant_id}:{sha256(sorted_collection_ids + ":" + query)}
  response:  rag:rsp:{tenant_id}:{pipeline_id}:{sha256(normalised_query)}
  invalidation set: rag:inv:{tenant_id}:{collection_id}  → SET of retrieval cache keys

Security:
  - Keys include tenant_id — cross-tenant cache pollution is impossible.
  - Cached values are JSON-serialised dicts; never contain raw document content beyond
    what was already returned to the user (chunk highlight text, citations).
  - Cache values are NOT logged — they may contain user query text or answer snippets.
  - Cache miss → normal pipeline; cache errors are logged and silently swallowed
    (degraded-gracefully: cache errors must never break the query path).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_RETRIEVAL_PREFIX = "rag:ret"
_RESPONSE_PREFIX = "rag:rsp"
_INVALIDATION_PREFIX = "rag:inv"

DEFAULT_RETRIEVAL_TTL = 1800  # seconds
DEFAULT_RESPONSE_TTL = 3600  # seconds


def _sha256_key(*parts: str) -> str:
    """Hash multiple string parts into a short hex digest for use in a Redis key."""
    combined = ":".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:32]


def _retrieval_key(tenant_id: uuid.UUID, collection_ids: list[uuid.UUID], query: str) -> str:
    sorted_ids = ",".join(sorted(str(c) for c in collection_ids))
    digest = _sha256_key(sorted_ids, query)
    return f"{_RETRIEVAL_PREFIX}:{tenant_id}:{digest}"


def _response_key(tenant_id: uuid.UUID, pipeline_id: uuid.UUID, query: str) -> str:
    normalised = query.strip().lower()
    digest = _sha256_key(normalised)
    return f"{_RESPONSE_PREFIX}:{tenant_id}:{pipeline_id}:{digest}"


def _invalidation_set_key(tenant_id: uuid.UUID, collection_id: uuid.UUID) -> str:
    return f"{_INVALIDATION_PREFIX}:{tenant_id}:{collection_id}"


class RAGCache:
    """Redis-backed cache for RAG retrieval results and generated responses.

    All public methods swallow exceptions and return None/False on error so
    that cache failures never block the query path.
    """

    def __init__(
        self,
        redis: Any,
        retrieval_ttl: int = DEFAULT_RETRIEVAL_TTL,
        response_ttl: int = DEFAULT_RESPONSE_TTL,
    ) -> None:
        self._redis = redis
        self._retrieval_ttl = retrieval_ttl
        self._response_ttl = response_ttl

    # ── Retrieval cache ───────────────────────────────────────────────────────

    async def get_retrieval(
        self,
        tenant_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        query: str,
    ) -> list[dict[str, Any]] | None:
        """Return cached Qdrant results or None on miss / error."""
        key = _retrieval_key(tenant_id, collection_ids, query)
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            chunks: list[dict[str, Any]] = json.loads(raw)
            logger.debug("cache.retrieval_hit", tenant_id=str(tenant_id), key_prefix=key[:40])
            return chunks
        except Exception as exc:
            logger.warning("cache.retrieval_get_error", error=type(exc).__name__)
            return None

    async def set_retrieval(
        self,
        tenant_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        query: str,
        chunks: list[dict[str, Any]],
        ttl: int | None = None,
    ) -> None:
        """Store Qdrant results in cache and register key in invalidation sets."""
        key = _retrieval_key(tenant_id, collection_ids, query)
        effective_ttl = ttl if ttl is not None else self._retrieval_ttl
        try:
            payload = json.dumps(chunks, default=str)
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.setex(key, effective_ttl, payload)
                # Register the key in each collection's invalidation set so that
                # document indexing can purge affected retrieval cache entries.
                for cid in collection_ids:
                    inv_key = _invalidation_set_key(tenant_id, cid)
                    pipe.sadd(inv_key, key)
                    pipe.expire(inv_key, effective_ttl + 60)
                await pipe.execute()
        except Exception as exc:
            logger.warning("cache.retrieval_set_error", error=type(exc).__name__)

    # ── Response cache ────────────────────────────────────────────────────────

    async def get_response(
        self,
        tenant_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        query: str,
    ) -> dict[str, Any] | None:
        """Return cached LLM response or None on miss / error."""
        key = _response_key(tenant_id, pipeline_id, query)
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            response: dict[str, Any] = json.loads(raw)
            logger.debug("cache.response_hit", tenant_id=str(tenant_id))
            return response
        except Exception as exc:
            logger.warning("cache.response_get_error", error=type(exc).__name__)
            return None

    async def set_response(
        self,
        tenant_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        query: str,
        answer: str,
        citations: list[dict[str, Any]],
        ttl: int | None = None,
    ) -> None:
        """Store LLM-generated answer in cache."""
        key = _response_key(tenant_id, pipeline_id, query)
        effective_ttl = ttl if ttl is not None else self._response_ttl
        try:
            payload = json.dumps({"answer": answer, "citations": citations}, default=str)
            await self._redis.setex(key, effective_ttl, payload)
        except Exception as exc:
            logger.warning("cache.response_set_error", error=type(exc).__name__)

    # ── Cache invalidation ────────────────────────────────────────────────────

    async def invalidate_collection(
        self,
        tenant_id: uuid.UUID,
        collection_id: uuid.UUID,
    ) -> int:
        """Delete all retrieval cache entries for a collection.

        Called by node_persist after a new document is indexed.
        Returns the number of keys deleted.
        """
        inv_key = _invalidation_set_key(tenant_id, collection_id)
        try:
            members = await self._redis.smembers(inv_key)
            if not members:
                return 0
            keys_to_delete = list(members) + [inv_key.encode()]
            deleted = await self._redis.delete(*keys_to_delete)
            logger.info(
                "cache.collection_invalidated",
                tenant_id=str(tenant_id),
                collection_id=str(collection_id),
                keys_deleted=deleted,
            )
            return int(deleted)
        except Exception as exc:
            logger.warning(
                "cache.invalidation_error",
                tenant_id=str(tenant_id),
                collection_id=str(collection_id),
                error=type(exc).__name__,
            )
            return 0


# ── Singleton ─────────────────────────────────────────────────────────────────

_rag_cache: RAGCache | None = None


def get_rag_cache() -> RAGCache:
    """Return the global RAGCache singleton (lazy init with default Redis client)."""
    global _rag_cache
    if _rag_cache is None:
        from src.core.clients.redis_client import get_redis_client

        _rag_cache = RAGCache(redis=get_redis_client())
    return _rag_cache
