# TASK-025: Document Version Control

**Status:** TODO
**Priority:** P2 — required for managing evolving medical protocols
**Owner:** backend-dev + rag-engineer
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/architecture.md`, `docs/data-model.md`, `docs/prd.md` §FR-2
**Estimated effort:** 5–6 days

---

## Overview

Medical procedures and clinical guidelines are updated regularly. Currently, re-uploading a document with the same name creates a duplicate. This task adds document versioning: when a user uploads a file with an identical name (or explicitly marks it as a new version), the system:

1. Creates a new `document_version` record linked to the existing document.
2. Re-runs the full ingest pipeline on the new version.
3. **Replaces** the old version's chunks in Qdrant with the new version's chunks.
4. Keeps the old version in MinIO and Postgres for audit/history — only the latest version is searchable.

---

## Usage

**Typical scenario:**
1. Admin uploads `procedura-wypisu-v1.pdf` — document created with `version=1`.
2. Protocol is updated. Admin uploads `procedura-wypisu-v2.pdf` to the same collection, same document name.
3. System detects a matching document (by filename + collection_id OR explicit `parent_document_id` param).
4. Creates version 2 — triggers re-ingest — replaces v1 chunks in Qdrant.
5. `GET /documents/{id}` returns the latest version; `GET /documents/{id}/versions` lists all.

---

## Database Schema Changes

```sql
-- New table for version history
CREATE TABLE document_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    minio_key TEXT NOT NULL,            -- path to this version's file in MinIO
    file_hash TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    ingestion_job_id UUID REFERENCES ingestion_jobs(id),
    status VARCHAR(20) NOT NULL DEFAULT 'processing',
    is_current BOOLEAN NOT NULL DEFAULT FALSE,   -- only one TRUE per document_id
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    note TEXT                           -- optional change note from uploader
);

CREATE UNIQUE INDEX uq_document_current_version
    ON document_versions(document_id) WHERE is_current = TRUE;

-- Extend documents table
ALTER TABLE documents
    ADD COLUMN current_version_number INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN version_count INTEGER NOT NULL DEFAULT 1;
```

Alembic migration: `0014_document_versioning.py`

---

## API Changes

### POST /collections/{collection_id}/documents (extended)

New optional body field: `parent_document_id: UUID | None` — explicitly link to an existing document as a new version.

If `parent_document_id` is null but a document with the same `original_filename` exists in the collection, the API asks:
```json
{
  "conflict": "document_exists",
  "existing_document_id": "...",
  "existing_version": 2,
  "message": "A document with this name already exists. Upload as new version or rename.",
  "actions": ["new_version", "new_document"]
}
```
HTTP 409. Client re-sends with `action=new_version` param.

### GET /documents/{document_id}/versions

**Response:**
```json
{
  "document_id": "...",
  "versions": [
    {
      "version_number": 2,
      "is_current": true,
      "status": "ready",
      "file_size_bytes": 2457600,
      "created_by_name": "Anna K.",
      "created_at": "2026-08-01T09:00:00Z",
      "note": "Updated contraindication section"
    },
    {
      "version_number": 1,
      "is_current": false,
      "status": "superseded",
      "created_at": "2026-07-01T10:00:00Z",
      "note": null
    }
  ]
}
```

### POST /documents/{document_id}/versions/{version_number}/restore

Restore an older version as current (re-runs indexing of old version's content).

**Permission:** `documents:upload` + document ownership or Admin.

---

## Ingest Graph Changes

`node_upsert.py` extended: when `state.parent_document_id` is set, before upserting new chunks, **delete all Qdrant points** with `document_id == parent_document_id AND version_number < current`. Uses `RetrievalService.delete_document_chunks()` (existing pattern from `DeletionService`).

**State additions:**
```python
parent_document_id: UUID | None = None
version_number: int = 1
```

---

## Qdrant Chunk Payload Change

Add `version_number: int` field to all chunk payloads. This enables filtering by version and targeted deletion of old chunks.

Existing chunks (version 1) get `version_number=1` via a migration script (best-effort, not blocking).

---

## Implementation Steps

1. Alembic migration: `document_versions` table + `documents` columns.
2. Modify upload endpoint: detect name conflict → return 409 with actions.
3. `DocumentRepository`:
   - `create_version(document_id, ...) -> DocumentVersion`
   - `set_current_version(document_id, version_number)` — atomic update + set old version `is_current=False`
   - `get_versions(document_id, tenant_id) -> list[DocumentVersion]`
4. Modify `node_upsert.py`: delete old Qdrant chunks before upsert when `parent_document_id` set.
5. Update `DeletionService`: when deleting a document, delete all versions from MinIO + Qdrant.
6. `restore_version()` service method: trigger re-ingest of old version's MinIO file.
7. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- Qdrant chunk deletion scoped by `tenant_id` + `document_id` — no cross-tenant deletion possible.
- Version restore is logged in `audit_log`.
- Old version files remain in MinIO (audit trail) — only removed on explicit document deletion (GDPR request).
- `parent_document_id` verified against `tenant_id` before linking.

---

## Tests

**Unit:**
- `set_current_version` correctly sets `is_current=TRUE` on new, `FALSE` on old
- `node_upsert` deletes old Qdrant points when `parent_document_id` is set
- 409 conflict returned when filename matches existing document

**Integration:**
- Upload v1 → upload v2 same name → only v2 chunks searchable
- Restore v1 → v1 chunks searchable again
- Delete document → all versions deleted from MinIO and Qdrant
- `@pytest.mark.tenant_isolation`: cannot link version to document from another tenant

---

## Definition of Done

- [ ] `document_versions` table + Alembic migration
- [ ] 409 conflict detection on upload with `new_version` action
- [ ] Version list endpoint
- [ ] Restore version endpoint
- [ ] `node_upsert` deletes old chunks before upserting new version
- [ ] `DeletionService` handles all versions
- [ ] `audit_log` entry on version restore
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Tenant isolation tests
