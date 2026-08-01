# TASK-029: Semantic Deduplication — Near-duplicate Detection via Embedding Similarity

**Status:** TODO
**Priority:** P2 — reduces noise in the knowledge base; improves retrieval quality
**Owner:** rag-engineer
**Reviewer:** python-reviewer
**Related docs:** `docs/prd.md` §FR-2 US-2.3, `docs/architecture.md` §ingest graph
**Estimated effort:** 3–4 days

---

## Overview

The current `node_validate` node checks for exact duplicates via `file_hash`. This misses:
- Same document re-uploaded as a different filename
- Slightly edited versions (corrected typos, reformatted headers)
- Documents copy-pasted from each other with small modifications

Near-duplicate documents inflate the knowledge base, cause redundant chunk retrieval, and waste storage/embedding compute.

This task upgrades deduplication to use **embedding similarity**: compute the document-level embedding (mean-pool of chunk embeddings), store it in `documents.embedding_vector`, and compare cosine similarity against existing documents in the same collection. Threshold: `similarity > 0.95` → flag as near-duplicate.

---

## Current State

`node_validate.py` already computes `file_hash` and compares against `documents.file_hash` in Postgres. This covers byte-identical duplicates.

---

## Changes

### Database

```sql
-- Add document-level embedding to documents table
ALTER TABLE documents
    ADD COLUMN embedding_vector VECTOR(768),    -- dimension matches collection embedding model
    ADD COLUMN dedup_similarity FLOAT,           -- highest similarity score found (for audit)
    ADD COLUMN dedup_similar_doc_id UUID REFERENCES documents(id);
                                                 -- most similar existing doc (for audit)

CREATE INDEX ix_documents_tenant_collection_embedding
    ON documents USING ivfflat (embedding_vector vector_cosine_ops)
    WHERE status NOT IN ('rejected', 'failed');
```

Uses `pgvector` extension (already likely available; verify in migration).

Alembic migration: `0016_document_embedding_dedup.py`

### Ingest Graph

New node: `node_semantic_dedup` — runs after `node_embed` (chunks are already embedded), before `node_upsert`.

```
node_embed → node_semantic_dedup → node_upsert
```

**`node_semantic_dedup` logic:**
1. Compute document embedding: mean-pool all chunk embeddings from `state.chunks`.
2. Store `document_embedding` in state.
3. Query Postgres: `SELECT id, embedding_vector <=> :doc_emb AS similarity FROM documents WHERE tenant_id=:tid AND collection_id=:cid AND status='ready' ORDER BY similarity ASC LIMIT 1`.
4. If `similarity > threshold` (configurable, default `0.95`): set `state.dedup_result = "near_duplicate"`, record `similar_doc_id`.
5. Save `dedup_similarity` and `dedup_similar_doc_id` to the document record.
6. If near-duplicate and `collections.reject_near_duplicates=True`: set `state.status = "rejected"`. Otherwise: set `state.status = "needs_review"` with `validation_result.issues += [{code: "NEAR_DUPLICATE"}]`.

### Config

`collections` table: add `reject_near_duplicates BOOLEAN DEFAULT FALSE` and `dedup_threshold FLOAT DEFAULT 0.95`.

Alembic migration: extend 0016 or separate `0016b`.

---

## State Changes (`ingest_graph/state.py`)

```python
document_embedding: list[float] | None = None
dedup_result: str | None = None         # "unique" | "exact_duplicate" | "near_duplicate"
dedup_similar_doc_id: UUID | None = None
dedup_similarity: float | None = None
```

---

## Implementation Steps

1. Verify `pgvector` extension in dev Postgres (`CREATE EXTENSION IF NOT EXISTS vector`).
2. Alembic migration: `embedding_vector`, `dedup_similarity`, `dedup_similar_doc_id` on `documents`; IVFFlat index.
3. Add `reject_near_duplicates`, `dedup_threshold` to `collections` (extend migration or separate).
4. Create `src/graphs/ingest_graph/nodes/node_semantic_dedup.py`:
   - Mean-pool `state.chunks[*].embedding` → document vector
   - pgvector cosine similarity query via SQLAlchemy
   - Set state fields based on result
5. Update routing: insert `node_semantic_dedup` between `node_embed` and `node_upsert`.
6. Update `node_validate.py` routing: if exact hash duplicate → short-circuit (skip semantic dedup).
7. Update `DocumentRepository` — save document embedding after dedup check.
8. Update `docs/02-Architektura.md` ingest graph diagram.

---

## Threshold Tuning

The default threshold of `0.95` is conservative (very similar only). Before deploying:
- Run against eval corpus (TASK-028 sample documents)
- Check precision/recall of near-duplicate detection
- Adjust per collection if needed (legal docs = strict 0.98; general docs = 0.93)

---

## Security

- Embedding vector never logged.
- `similar_doc_id` in `dedup_similar_doc_id` is only meaningful within same tenant/collection — cross-tenant query is impossible (WHERE clause includes `tenant_id`).
- Admin can see `dedup_similarity` in the review panel (useful for deciding whether to approve a flagged near-duplicate).

---

## Tests

**Unit:**
- Mean-pooling of chunk embeddings produces correct document vector
- `similarity > 0.95` triggers near-duplicate flag
- `similarity < 0.95` → state unchanged, document proceeds
- Exact hash duplicate short-circuits before semantic dedup

**Integration:**
- Upload same PDF content with different filename → flagged as near-duplicate
- Two different documents → both indexed without conflict
- `reject_near_duplicates=True` → document status is `rejected` immediately
- `reject_near_duplicates=False` → document goes to `needs_review` with `NEAR_DUPLICATE` issue

---

## Definition of Done

- [ ] `pgvector` extension verified/enabled
- [ ] `embedding_vector`, `dedup_similarity`, `dedup_similar_doc_id` columns + IVFFlat index
- [ ] `reject_near_duplicates`, `dedup_threshold` on `collections`
- [ ] `node_semantic_dedup` implemented and tested in isolation
- [ ] Routing updated: `node_embed → node_semantic_dedup → node_upsert`
- [ ] Exact hash duplicate still short-circuits (no regression)
- [ ] `docs/02-Architektura.md` ingest diagram updated
- [ ] Unit + integration tests
- [ ] Threshold documented and configurable per collection
