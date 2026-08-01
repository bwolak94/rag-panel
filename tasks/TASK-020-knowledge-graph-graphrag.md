# TASK-020: Knowledge Graph / GraphRAG — Entity-Aware Retrieval

**Status:** TODO
**Priority:** P2 — advanced retrieval quality for complex multi-hop queries
**Owner:** rag-engineer + ml-engineer
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/architecture.md`, `docs/reference-repos.md`, `docs/prd.md` §FR-4
**Estimated effort:** 8–12 days

---

## Overview

Complex medical queries often require multi-hop reasoning: "What is the contraindication for Drug X in patients with condition Y?" A single chunk rarely contains the full answer. GraphRAG builds a knowledge graph from ingested documents and enables entity-relationship-aware retrieval.

This task implements:
1. **Entity extraction** during ingest — new `node_extract_entities` ingest graph node.
2. **Graph storage** — entities and relationships stored in PostgreSQL (`entities`, `entity_relations` tables).
3. **Graph retrieval** — new `node_retrieve_graph` query graph node that augments chunk retrieval with graph context.
4. The feature is **opt-in per collection** via `graph_enabled: bool`.

---

## Architecture

### Ingest side
```
... → node_upsert → [node_extract_entities if graph_enabled] → END
```

`node_extract_entities`:
- Calls LLM with a structured extraction prompt (versioned in `graphs/prompts/`)
- Extracts: entities (name, type, description) + relations (subject, predicate, object)
- Upserts to `entities` and `entity_relations` tables with `tenant_id` + `collection_id` + `document_id`

### Query side
```
retrieve → [node_retrieve_graph if graph_enabled] → grade_documents → generate
```

`node_retrieve_graph`:
- Extracts entities from the rewritten query (LLM or NER)
- Queries `entity_relations` for connected entities
- Fetches related chunks from Qdrant (by `document_id` filter)
- Merges with vector retrieval results (deduplication by chunk_id)

---

## Tech Stack

- **Entity extraction LLM:** abstracted via `models_registry` (same client as query graph)
- **Prompt:** `graphs/prompts/entity_extraction_v1.txt` — versioned
- **Graph DB:** PostgreSQL (no Neo4j — keep stack simple for MVP)
- **NER alternative:** `spacy` with `pl_core_news_lg` model for fast entity recognition (no LLM call)
- **Config:** `collections.graph_enabled BOOLEAN DEFAULT FALSE`
- **Langfuse:** trace `entity_extraction` and `graph_retrieval` spans

---

## Database Schema

```sql
-- New tables
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entity_type VARCHAR(50) NOT NULL,  -- DRUG, CONDITION, PROCEDURE, PERSON, etc.
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE entity_relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    collection_id UUID NOT NULL,
    source_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate VARCHAR(100) NOT NULL,  -- e.g. "CONTRAINDICATED_WITH", "TREATS", "REQUIRES"
    chunk_id TEXT NOT NULL,           -- Qdrant point_id where relation was found
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    confidence FLOAT NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ix_entities_tenant_collection ON entities(tenant_id, collection_id);
CREATE INDEX ix_entity_relations_source ON entity_relations(source_entity_id);
CREATE INDEX ix_entity_relations_tenant ON entity_relations(tenant_id, collection_id);
```

Alembic migration: `0010_knowledge_graph_tables.py`

---

## Prompt (versioned)

`graphs/prompts/entity_extraction_v1.txt`:
```
Extract named entities and their relationships from the following document chunk.

Output JSON only:
{
  "entities": [
    {"name": "...", "type": "DRUG|CONDITION|PROCEDURE|PERSON|OTHER", "description": "..."}
  ],
  "relations": [
    {"subject": "...", "predicate": "...", "object": "...", "confidence": 0.0-1.0}
  ]
}

Document chunk:
---
{chunk_text}
---
```

---

## Implementation Steps

1. Alembic migration: `entities`, `entity_relations` tables.
2. Add `graph_enabled` to `collections` table (separate migration or extend 0010).
3. Create `src/graphs/ingest_graph/nodes/node_extract_entities.py`.
4. Create `EntityRepository` (`src/db/repositories/entity_repository.py`) — upsert entities/relations with tenant filter.
5. Update ingest graph routing: add `node_extract_entities` as optional final step.
6. Create `src/graphs/query_graph/nodes/node_retrieve_graph.py`.
7. Update query graph routing: insert `node_retrieve_graph` between `retrieve` and `grade_documents`.
8. Version prompt in `graphs/prompts/`.
9. Register `entity_extraction` and `graph_retrieval` Langfuse spans.
10. Update `DeletionService` — cascade delete entities/relations for document deletion (GDPR Art. 17).
11. Update `docs/02-Architektura.md` graph diagrams (both ingest and query).

---

## Security

- Entities table has `tenant_id` — all queries filtered by tenant (same pattern as documents).
- Entity extraction LLM output is untrusted — parsed as JSON, never executed.
- Chunk text sent to LLM for extraction follows existing guardrails (no PII logged).
- `/skill /deletion-cascade` checklist: entities and relations must be deleted when document is deleted.

---

## Tests

**Unit:**
- `node_extract_entities` parses LLM JSON output correctly; handles malformed JSON gracefully
- `node_retrieve_graph` merges graph context with vector results without duplicates
- `EntityRepository.upsert()` filters by `tenant_id`

**Integration:**
- Ingest document with known entities → entities appear in DB
- Query for entity → graph retrieval finds related documents
- Delete document → entities and relations cascade-deleted
- `@pytest.mark.tenant_isolation`: entity query from tenant A cannot access tenant B entities

---

## Definition of Done

- [ ] `node_extract_entities` and `node_retrieve_graph` implemented and tested
- [ ] `entities` and `entity_relations` tables with Alembic migration
- [ ] `graph_enabled` per collection (default `False`)
- [ ] `DeletionService` updated for cascade
- [ ] Prompt versioned in `graphs/prompts/`
- [ ] Langfuse spans for both nodes
- [ ] Tenant isolation tests pass
- [ ] `docs/02-Architektura.md` updated (both graph diagrams)
- [ ] `/skill /prompt-version` changelog entry
- [ ] `/skill /deletion-cascade` checklist completed
