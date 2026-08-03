# TASK-023: Bulk Document Import — ZIP Upload and Folder Sync

**Status:** DONE
**Priority:** P2 — needed for tenant onboarding (initial knowledge base population)
**Owner:** backend-dev + rag-engineer
**Reviewer:** python-reviewer
**Related docs:** `docs/prd.md` §FR-3 (onboarding), `docs/architecture.md` §ingest
**Estimated effort:** 4–5 days

---

## Overview

Initial onboarding of a new tenant requires importing tens to hundreds of documents at once. Uploading one-by-one via the documents API is impractical. This task adds bulk import capabilities:

1. **ZIP upload:** user uploads a `.zip` file containing documents; the API extracts and enqueues each file individually.
2. **MinIO bucket sync:** admin triggers a sync from an existing MinIO bucket prefix (e.g., after manual bulk upload via `mc` CLI).

Both flows create one `ingestion_job` per document and stream progress via SSE or polling endpoint.

---

## Usage

**Actor:** Admin or Contributor with `documents:upload` permission.

**ZIP upload scenario:**
1. Admin zips 80 PDFs locally.
2. `POST /collections/{id}/documents/bulk-import` with `Content-Type: multipart/form-data`, field `archive`.
3. API validates ZIP (max 500 MB), extracts filenames, validates each file type and size.
4. Creates a `bulk_import_job` record with total file count.
5. Enqueues each file to MinIO + Redis Streams.
6. Returns `bulk_import_job_id`.
7. Admin polls `GET /collections/{id}/bulk-import/{job_id}/status` for progress.

**Bucket sync scenario:**
1. Admin has uploaded files via `mc cp` to `s3://tenant-bucket/collection-prefix/`.
2. `POST /collections/{id}/documents/bucket-sync` — triggers scan of MinIO prefix.
3. API lists objects, creates ingestion jobs for new (not yet indexed) files.

---

## Database Schema

```sql
CREATE TABLE bulk_import_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    created_by UUID NOT NULL REFERENCES users(id),
    source_type VARCHAR(20) NOT NULL,   -- 'zip_upload' | 'bucket_sync'
    total_files INTEGER NOT NULL DEFAULT 0,
    queued_files INTEGER NOT NULL DEFAULT 0,
    succeeded_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,  -- duplicates
    status VARCHAR(20) NOT NULL DEFAULT 'processing',  -- processing | completed | failed
    error_summary JSONB,                -- list of {filename, error} for failed files
    created_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);
```

Alembic migration: `0012_bulk_import_jobs.py`

---

## API Endpoints

### POST /collections/{collection_id}/documents/bulk-import

**Body:** `multipart/form-data` — field `archive` (ZIP file, max 500 MB)

**Validation:**
- ZIP must be < 500 MB total
- Max 200 files per import (configurable in settings)
- Accepted file types per collection config
- Each extracted file <= 100 MB (existing limit)
- Filenames sanitized (strip path traversal, Unicode-normalize)

**Response 202:**
```json
{
  "bulk_import_job_id": "...",
  "total_files": 82,
  "status": "processing",
  "status_url": "/api/v1/collections/{id}/bulk-import/{job_id}/status"
}
```

### GET /collections/{collection_id}/bulk-import/{job_id}/status

**Response:**
```json
{
  "id": "...",
  "status": "processing",
  "total_files": 82,
  "queued_files": 82,
  "succeeded_files": 61,
  "failed_files": 3,
  "skipped_files": 1,
  "progress_pct": 78,
  "failed_details": [
    {"filename": "scan001.pdf", "error": "Duplicate document"},
    {"filename": "bad.exe", "error": "Unsupported file type"}
  ],
  "created_at": "...",
  "completed_at": null
}
```

### POST /collections/{collection_id}/documents/bucket-sync

**Body:** `{}` (no params — syncs the entire collection prefix)

**Response 202:** same shape as bulk-import response.

---

## Tech Stack

- **ZIP extraction:** `zipfile` (stdlib) — extract to temp dir (`tempfile.TemporaryDirectory`)
- **Path traversal protection:** reject entries where `entry.filename` starts with `/` or contains `..`
- **MinIO listing:** `minio_client.list_objects(prefix=f"{tenant_id}/{collection_id}/")`
- **Background processing:** `asyncio.create_task` for extraction loop; per-file ingest via existing Redis Streams
- **Temp storage:** extracted files written to `/tmp/bulk_import_{job_id}/` — cleaned up after enqueue
- **Progress updates:** `bulk_import_jobs` counters incremented atomically per file as ingest worker reports completion

---

## Implementation Steps

1. Alembic migration: `bulk_import_jobs` table.
2. `src/domain/bulk_import_service.py`:
   - `create_zip_import(collection_id, archive_bytes, ctx) -> BulkImportJob`
   - `create_bucket_sync(collection_id, ctx) -> BulkImportJob`
   - `get_job_status(job_id, tenant_id) -> BulkImportJob`
3. ZIP extraction logic with path traversal protection.
4. Per-file validation (type, size) — reject and record in `error_summary`, continue.
5. Enqueue valid files: `minio_client.put_object()` → Redis Streams event (reuse existing flow).
6. Ingest worker: after job completion, update `bulk_import_jobs` counter via `job_id` correlation.
7. Router `src/api/routers/bulk_import.py`, register in `main.py`.
8. Cleanup task: delete temp directories after all files enqueued.
9. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- ZIP bomb protection: check uncompressed size ratio before full extraction (ratio > 100:1 → reject).
- Max file count enforced at extraction time (before any I/O).
- Filenames sanitized: `pathlib.Path(entry.filename).name` — strips directory components.
- All files go through the existing ingest pipeline (validation, PII scan, dedup).
- `tenant_id` and `collection_id` from JWT context — never from ZIP content.
- Temp dir cleaned up even on error (try/finally).

---

## Tests

**Unit:**
- ZIP extraction rejects path traversal entries (`../../etc/passwd`)
- ZIP bomb detected (compressed ratio > 100:1)
- File count limit enforced
- `create_bucket_sync` lists only objects in tenant/collection prefix

**Integration:**
- Upload ZIP with 5 PDFs → 5 ingestion jobs created → `bulk_import_jobs.total_files=5`
- Progress endpoint returns correct counts
- Invalid file in ZIP → skipped with error recorded, others proceed
- Cross-tenant: admin A cannot get status of admin B's bulk import job

---

## Definition of Done

- [ ] `bulk_import_jobs` table + Alembic migration
- [ ] ZIP upload with path traversal + ZIP bomb protection
- [ ] Bucket sync endpoint
- [ ] Progress polling endpoint
- [ ] Temp cleanup on success and error
- [ ] All files run through existing ingest pipeline
- [ ] Tenant isolation (job status scoped to tenant)
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests
