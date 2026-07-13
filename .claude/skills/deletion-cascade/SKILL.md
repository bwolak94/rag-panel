---
name: deletion-cascade
description: Implements or verifies cascading data deletion (GDPR Art. 17) via DeletionService. Use when adding a new data type or handling a user/tenant deletion request.
---

# Cascading Data Deletion (GDPR Art. 17) — procedure

1. Read `docs/05-Bezpieczenstwo-RODO.md` — section "Right to Erasure".
2. Read `src/domain/deletion_service.py` — understand existing deletion flows.

3. **Resource deletion mapping** (for a given scope: user / document / tenant):

   | Layer    | What to delete                              | Method                            |
   |----------|---------------------------------------------|-----------------------------------|
   | Postgres | Rows in business tables (CASCADE)           | SQLAlchemy delete + flush         |
   | Qdrant   | Points with `filter={tenant_id, doc_id}`    | RetrievalService.delete_points()  |
   | MinIO    | Objects in bucket `<tenant_id>/...`         | StorageClient.delete_objects()    |
   | Redis    | Cache keys with tenant prefix               | CacheClient.delete_pattern()      |

4. **Implementation in DeletionService:**
   - One DB transaction + compensating saga for Qdrant/MinIO (if Postgres OK but Qdrant fails → log + retry job).
   - Add the new resource type as a new method `delete_<resource>(ctx, resource_id)`.
   - Note: deleting a tenant = first all documents → Qdrant collections → MinIO bucket → tenant record.

5. **Idempotency:** calling delete twice must not raise an error (check existence before deletion or ignore "not found").

6. **Audit:** every deletion writes to `audit_log` (who, what, when) — after PII is removed but before the audit_log record itself is deleted.

7. Tests in `tests/integration/test_deletion.py`:
   - Delete a document → verify Postgres, Qdrant, MinIO (no traces remain).
   - Delete a tenant → verify isolation (another tenant's resources untouched).
   - Idempotency: double deletion without error.

8. Run `/rodo-audit` after implementation to verify the Art. 17 checklist.
