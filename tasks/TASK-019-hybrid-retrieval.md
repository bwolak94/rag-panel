# TASK-019: Hybrid Retrieval — BM25 + Dense Vector with RRF

**Status:** TODO
**Priority:** P1 — retrieval quality improvement; mentioned in roadmap Phase 3
**Owner:** rag-engineer
**Reviewer:** python-reviewer (security profile) + security-auditor
**Related docs:** `docs/architecture.md`, `docs/roadmap.md` Phase 3, `docs/reference-repos.md`
**Estimated effort:** 4–6 days

---

## Overview

The current retrieval uses dense vector similarity only. This misses exact keyword matches that BM25 excels at — important for medical terminology (drug names, ICD codes, procedure codes) where semantics alone is insufficient.

This task implements **hybrid retrieval**: run BM25 (sparse) and dense vector search in parallel, then merge results using **Reciprocal Rank Fusion (RRF)**. Qdrant supports sparse vectors natively via `SparseVector`; we use the `fastembed` sparse encoder (SPLADE or BM25 model).

All retrieval remains in `RetrievalService` — no direct Qdrant access elsewhere.

---

## Architecture

```
query → [dense encoder] → dense vector search (Qdrant)
      → [sparse encoder] → sparse vector search (Qdrant)
      → RRF merge (k=60) → top_k=8 results → grade_documents node
```

RRF score: `sum(1 / (k + rank_i))` for each document across result lists.

---

## Tech Stack

- **Qdrant:** `SparseVector` field in collection, `NamedSparseVector` in search query
- **Sparse encoder:** `fastembed` `Qdrant/bm25` model (local, no GPU needed) — run in `ThreadPoolExecutor`
- **Dense encoder:** unchanged (current embedding model per collection)
- **Config:** `collections.retrieval_config` extended with `retrieval_mode: "dense" | "sparse" | "hybrid"` (default `"hybrid"`)
- **RRF k param:** in `Settings` as `RETRIEVAL_RRF_K: int = 60`

---

## RetrievalService Changes (`src/retrieval/`)

```python
# New method signature
async def hybrid_search(
    self,
    query: str,
    tenant_id: UUID,
    collection_ids: list[UUID],
    top_k: int = 8,
    score_threshold: float = 0.0,
) -> list[RetrievedChunk]:
    # 1. Encode query: dense (async) + sparse (ThreadPoolExecutor)
    # 2. Qdrant prefetch: dense search top_k*2, sparse search top_k*2
    # 3. RRF merge → top_k
    # 4. Filter by score_threshold
    # 5. Return list[RetrievedChunk]
```

`retrieve()` public method dispatches to `hybrid_search()` or `dense_search()` based on `collection.retrieval_mode`.

---

## Qdrant Collection Schema Change

Existing collections must be **re-indexed** to add the sparse vector field. New collections created after this task include sparse vectors by default.

- Migration script: `scripts/migrate_collections_hybrid.py` — re-index all existing collections with sparse vectors
- `/skill /reindex-collection` checklist required for each collection

**Point payload unchanged.** Only the vector storage adds a sparse field:
```python
vectors_config={
    "dense": VectorParams(size=..., distance=Distance.COSINE),
},
sparse_vectors_config={
    "sparse": SparseVectorParams(),
}
```

---

## Database Changes

- `collections` table: add `retrieval_mode VARCHAR(10) DEFAULT 'hybrid'`
- Alembic migration: `0009_hybrid_retrieval_config.py`

---

## Implementation Steps

1. Alembic migration: add `retrieval_mode` to `collections`.
2. Add `SparseVectorParams` to Qdrant collection creation in `RetrievalService`.
3. Implement `_encode_sparse(query: str) -> SparseVector` using `fastembed` BM25 in `ThreadPoolExecutor`.
4. Implement `_rrf_merge(dense_results, sparse_results, k=60) -> list` utility.
5. Implement `hybrid_search()` using Qdrant `prefetch` + `Query`.
6. Update `retrieve()` dispatch logic based on `collection.retrieval_mode`.
7. Update `node_retrieve.py` — no interface change (calls `RetrievalService.retrieve()`).
8. Write re-index migration script.
9. Update `docs/02-Architektura.md`.

---

## Security

- Tenant isolation: unchanged — sparse search uses same `tenant_id` filter in Qdrant payload.
- New `retrieval_mode` config comes from `collections` table (admin-set), never from user input.

---

## Tests

**Unit (`tests/unit/retrieval/test_hybrid_retrieval.py`):**
- `_rrf_merge` correctly scores and ranks across two result lists
- `hybrid_search` calls both dense and sparse search with correct tenant filter
- `retrieve_mode="dense"` skips sparse encoding

**Integration:**
- Query for exact ICD code returns correct document at rank 1
- Hybrid outperforms dense-only on keyword-heavy queries (fixture dataset)
- Cross-tenant isolation: sparse search does not return results from other tenants

**`@pytest.mark.tenant_isolation`** — required

---

## Definition of Done

- [ ] `hybrid_search()` implemented in `RetrievalService`
- [ ] `_rrf_merge()` unit-tested
- [ ] `retrieval_mode` configurable per collection
- [ ] Qdrant sparse vector support in collection creation
- [ ] Re-index script tested against dev Qdrant
- [ ] Tenant isolation tests pass
- [ ] `docs/02-Architektura.md` updated
- [ ] `/skill /reindex-collection` checklist completed
- [ ] `/skill /tenant-isolation-check` passed
