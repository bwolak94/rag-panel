---
name: reindex-collection
description: Re-indexes a Qdrant collection after an embedding model change. Use when changing the embedding model for a collection — never mix vectors from different models.
---

# Qdrant Collection Re-indexing — procedure

1. **Verify the need to re-index** — a change to the embedding model for a collection is ALWAYS required when:
   - `model_name` or `model_version` in `models_registry` changes for that collection.
   - Vector dimensionality (`vector_size`) changes.
   - Never continue ingesting into an existing collection with a new model.

2. Read `docs/04-Model-Danych.md` — collection configuration and the current embedding model.

3. **Re-indexing plan** (present to the user for approval before executing):
   - Old collection name: `<tenant_id>_<collection_name>`
   - New collection: `<tenant_id>_<collection_name>_v<N+1>` (do not delete the old one until verified)
   - Estimated re-indexing time (number of documents × avg embedding time)

4. Create the new collection via `RetrievalService` with the new configuration in `models_registry`.

5. Run re-indexing via the ingest worker:
   ```bash
   python -m src.ingest.reindex_worker --collection <name> --new-model <model_id>
   ```
   The worker must: (a) read chunks from Postgres, (b) compute new embeddings, (c) write to the new collection with the same deterministic `point_id`.

6. Post-re-indexing verification:
   - Compare point counts: old collection vs new.
   - Run `pytest tests/eval/ -q` — faithfulness and recall must be >= baseline.

7. Switch traffic to the new collection (update `active_collection` in tenant config).

8. Old collection: keep for 7 days (rollback window), then delete via `DeletionService`.

9. Update `models_registry` and `docs/04-Model-Danych.md` with the new configuration.
