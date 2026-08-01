# TASK-030: Retrieval Result Caching — Redis Semantic Cache

**Status:** TODO
**Priority:** P2 — reduces LLM inference latency and GPU cost for repeated queries
**Owner:** rag-engineer + backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md` §retrieval, `docs/prd.md` §FR-4
**Estimated effort:** 3–4 days

---

## Overview

In a clinic, the same questions are asked repeatedly: "What is the drug interaction between X and Y?", "What are the RODO data retention rules?", "What is the procedure for patient discharge?" Every occurrence triggers full Qdrant retrieval + LLM generation — expensive and slow (1–3s per query).

This task implements a **two-level cache**:
1. **Retrieval cache** — cache Qdrant results for (query_embedding, collection_ids, filters) → reuse chunks if semantically equivalent query found (cosine similarity > 0.97).
2. **Response cache** — cache final LLM-generated answers for exact (query_text_hash, pipeline_id) — TTL 1 hour.

Both caches are opt-in per pipeline (`cache_retrieval`, `cache_responses`).

---

## Cache Architecture

```
User query
    ↓
[Response cache check] → HIT → return cached answer (< 10ms)
    ↓ MISS
[Query rewriting] → [Retrieval cache check] → HIT → skip Qdrant, use cached chunks
    ↓ MISS
[Qdrant retrieval] → store in retrieval cache
    ↓
[LLM generation] → store in response cache
    ↓
Answer
```

---

## Cache Keys

### Response Cache
```
response_cache:{tenant_id}:{pipeline_id}:{sha256(normalized_query)}
```
Value: `{answer, sources, generated_at}` serialized as JSON.
TTL: 3600s (configurable via `pipeline.cache_ttl_seconds`).

### Retrieval Cache
```
retrieval_cache:{tenant_id}:{collection_ids_hash}:{embedding_vector_quantized}
```
Semantic matching: store embedding in Redis as float32 bytes; on query, compute cosine similarity against recent cache entries using a Redis sorted set (approximate, last 1000 queries per tenant).

> MVP simplification: retrieval cache uses exact `sha256(rewritten_query + collection_ids)` — no semantic matching. Semantic retrieval cache is a Phase 2 enhancement.

---

## Implementation

### Cache Layer (`src/core/cache.py`)

```python
class RAGCache:
    def __init__(self, redis: Redis, settings: Settings): ...

    async def get_response(
        self, tenant_id: UUID, pipeline_id: UUID, query: str
    ) -> CachedResponse | None: ...

    async def set_response(
        self, tenant_id: UUID, pipeline_id: UUID, query: str,
        answer: str, sources: list[dict], ttl: int = 3600
    ) -> None: ...

    async def get_retrieval(
        self, tenant_id: UUID, collection_ids: list[UUID], query: str
    ) -> list[RetrievedChunk] | None: ...

    async def set_retrieval(
        self, tenant_id: UUID, collection_ids: list[UUID], query: str,
        chunks: list[RetrievedChunk], ttl: int = 1800
    ) -> None: ...

    async def invalidate_collection(self, tenant_id: UUID, collection_id: UUID) -> None:
        """Call when documents are added/removed from collection."""
```

### Query Graph Integration

**`node_retrieve.py`** — check retrieval cache before calling `RetrievalService`:
```python
cached = await cache.get_retrieval(tenant_id, collection_ids, state.rewritten_query)
if cached:
    state.retrieved_chunks = cached
    state.cache_hit = "retrieval"
    return state
# ... normal retrieval ...
await cache.set_retrieval(...)
```

**`node_generate.py`** — NOT cached here (LLM already called). Response cache is checked in `ChatService` before invoking the graph:
```python
cached = await cache.get_response(ctx.tenant_id, pipeline_id, user_query)
if cached:
    return cached_response  # skip entire graph
```

### Cache Invalidation

When a new document reaches `status=ready` in a collection, invalidate all retrieval cache entries for that collection:
```python
await cache.invalidate_collection(tenant_id, collection_id)
```

Hooked into `node_upsert.py` after successful indexing.

---

## Pipeline Config Changes

`pipelines.config JSONB` — add:
```json
{
  "cache_retrieval": true,
  "cache_responses": false,
  "cache_ttl_seconds": 3600,
  "cache_retrieval_ttl_seconds": 1800
}
```

Response cache defaults `false` (conservative — medical answers should be fresh).

---

## Cache Observability

- Langfuse: add `cache_hit: "retrieval" | "response" | null` to trace metadata.
- Structured log: `{"event": "cache_hit", "type": "retrieval", "tenant_id": "...", "latency_ms": 3}` — no query content logged.
- `GET /admin/cache/stats` endpoint: hit rate, miss rate, entries count per tenant.

---

## Implementation Steps

1. Create `src/core/cache.py` with `RAGCache` class.
2. Add `RAGCache` to app lifespan (alongside Redis client).
3. Hook `get_response` / `set_response` in `ChatService._invoke_graph()`.
4. Hook `get_retrieval` / `set_retrieval` in `node_retrieve.py`.
5. Hook `invalidate_collection` in `node_upsert.py`.
6. Add `cache_retrieval`, `cache_responses`, `cache_ttl_seconds` to pipeline config schema.
7. Langfuse trace: add `cache_hit` field to all query traces.
8. `GET /admin/cache/stats` endpoint (simple Redis key count scan).
9. Unit + integration tests.

---

## Security

- Cache keys include `tenant_id` — no cross-tenant cache poisoning.
- `cache.invalidate_collection()` scoped to `tenant_id` — cannot invalidate another tenant's cache.
- Response cache stores answer text — treated as sensitive; keys are hashed (SHA-256), values are compressed JSON in Redis, no PII logged.
- Cache TTL ensures stale medical data is not served indefinitely (max 1h for responses).

---

## Tests

**Unit:**
- `get_response()` returns `None` on cache miss; correct value on hit
- `set_response()` stores with correct TTL
- `invalidate_collection()` deletes correct keys (mock Redis SCAN + DEL)
- Cache key is tenant-scoped (different tenant_id → different key)

**Integration:**
- Same query twice → second response served from cache (< 50ms vs > 500ms)
- New document ingested → retrieval cache invalidated → next query hits Qdrant
- Cache hit logged in Langfuse trace metadata
- `cache_responses=false` pipeline → response never cached even after generation

---

## Definition of Done

- [ ] `RAGCache` class implemented with response + retrieval cache
- [ ] Response cache checked in `ChatService` before graph invocation
- [ ] Retrieval cache checked in `node_retrieve` before Qdrant call
- [ ] Cache invalidation on new document indexed
- [ ] `cache_hit` in Langfuse trace
- [ ] `GET /admin/cache/stats` endpoint
- [ ] Cache keys tenant-scoped
- [ ] No query content or answer text in application logs
- [ ] Unit + integration tests (cache hit, miss, invalidation)
