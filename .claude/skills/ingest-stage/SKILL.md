---
name: ingest-stage
description: Adds or modifies a stage in the document ingestion pipeline (Redis Streams → worker → Qdrant). Use when changing text extraction, chunking, embeddings, or file validation.
---

# Ingest Pipeline Stage — procedure

1. Read `docs/02-Architektura.md` — ingest pipeline diagram (upload → Redis Stream → worker → extract → chunk → embed → Qdrant).
2. Check `src/ingest/worker.py` and `src/graphs/ingest_graph/` — understand the existing stages.

3. **Idempotency rule** (hard rule):
   - Every stage must be idempotent: re-running on the same document = same result.
   - `point_id` in Qdrant = deterministic hash of `(doc_id, chunk_index)` — never a random UUID.
   - Upsert instead of insert for chunks.

4. **New stage as an ingest graph node:**
   - Create `src/graphs/ingest_graph/nodes/<name>.py` — function `node_<name>(state: IngestState) -> dict`.
   - Ingest state: `IngestState` in `src/graphs/ingest_graph/state.py` — add new fields only when necessary.
   - Register the node in `src/graphs/ingest_graph/graph.py` with error handling (retry 3x with backoff, after 3 → status `failed`).

5. **Per-stage status** — each stage updates `ingestion_jobs`:
   ```python
   await job_service.update_status(job_id, stage="<name>", status="running")
   # ... logic ...
   await job_service.update_status(job_id, stage="<name>", status="done", meta={...})
   ```

6. **File validation** (if the stage handles uploads):
   - MIME: validate via `python-magic` (not by file extension).
   - Size: ≤ 100 MB.
   - Filename: `pathlib.Path(filename).name` (sanitize).
   - File must never be executed.

7. Tests in `tests/unit/ingest/test_<name>.py`:
   - Success with a sample document.
   - Idempotency: two runs = same result in Qdrant (no duplicate points).
   - Bad input → status `failed`, not an uncaught exception.

8. Run the full pipeline on a test file: `docker compose up -d && python -m src.ingest.test_run --file tests/fixtures/sample.pdf`.
9. Update the pipeline diagram in `docs/02-Architektura.md`.
