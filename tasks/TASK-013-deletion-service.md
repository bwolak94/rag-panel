# TASK-013: DeletionService (GDPR Art. 17)

**Status:** TODO
**Priority:** P0 — GDPR compliance blocker
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile) + security-auditor
**Related docs:** `docs/architecture.md` §13, §5 (Cross-Store Consistency) | `docs/data-model.md` §2.2, §5 | `docs/rodo.md`
**Estimated effort:** 3–4 days

---

## Overview

Implement `DeletionService` in `src/domain/deletion_service.py` as the single orchestrator for all GDPR Art. 17 cascading deletion operations across Postgres, Qdrant (via `RetrievalService`), and MinIO.

The deletion cascade order follows `docs/architecture.md` §5 (Cross-Store Consistency Rules). Postgres is the source of truth — it is written first (status change) and last (audit confirmation). Qdrant and MinIO deletions happen in between. On partial failure, Postgres status is set to `deletion_failed` so the operation can be retried and the state is always visible.

No data is hard-deleted from Postgres without completing the Qdrant and MinIO deletions first, except for cascades where Postgres ON DELETE CASCADE handles child rows automatically.

Any new data store added to the platform must be registered here (per `docs/architecture.md` §5 constraint).

---

## Usage

```python
from src.domain.deletion_service import DeletionService
from src.retrieval.schemas import TenantContext

svc = DeletionService(db=db, retrieval=retrieval_svc, minio=minio_client)

# Delete one document (called from DELETE /documents/{id} endpoint)
report = await svc.delete_document(
    ctx=TenantContext(tenant_id=tenant_id, allowed_collection_ids=[]),
    document_id=document_id,
)

# Delete a collection and all its documents
report = await svc.delete_collection(ctx, collection_id=collection_id)

# Full tenant offboarding (Owner or system only)
report = await svc.delete_tenant(ctx, tenant_id=tenant_id)
```

---

## Tech Stack

- **SQLAlchemy 2.0** async — all Postgres operations via `AsyncSession`
- **RetrievalService** — Qdrant deletion (TASK-009); no direct `qdrant_client` usage
- **minio-py** — MinIO object deletion
- **tenacity** — retry on Qdrant/MinIO failures (3 attempts, exponential backoff per `docs/architecture.md` §16)
- **structlog** — structured logging with `tenant_id`, `document_id`, `collection_id`; no content

---

## Database Patterns

### Deletion cascade order (from `docs/architecture.md` §5)

```
1. Postgres: document.status = "deleted"          (visible to users immediately — stops new queries)
2. Qdrant: delete all points for document_id       (via RetrievalService.delete_by_document)
3. MinIO: delete raw/ and processed/ objects       (removes source file)
4. Postgres: DELETE chunks_registry WHERE document_id = :id  (FK cascade also handles this)
5. Postgres: audit_log INSERT (action=data.deletion_confirmed)
```

On Qdrant/MinIO failure: retry up to 3 times. After 3 failures: set `document.status = deletion_failed`, insert `audit_log` with error details, add to `DeletionReport.errors`.

### Tables affected

**`documents`** — `status` changed to `deleted` (step 1) or `deletion_failed` (on error). `minio_key` set to NULL after successful MinIO deletion. Soft-delete only — row not removed from Postgres.

**`chunks_registry`** — Rows deleted after successful Qdrant deletion (step 4). The `ON DELETE CASCADE` on `document_id` FK means this also happens automatically if the document row is hard-deleted, but we do it explicitly here to maintain consistency ordering.

**`message_sources`** — `document_id` becomes NULL via `ON DELETE SET NULL` when document is deleted. `chunk_id` becomes NULL via `ON DELETE SET NULL`. Citation history is preserved; sources show "document deleted" in UI.

**`ingestion_jobs`** — `ON DELETE CASCADE` on `document_id` handles these automatically when the document row would be removed. Since we soft-delete documents, ingestion_jobs remain for the audit trail.

**`audit_log`** — Written at step 5. Two entries per document: one for the deletion request (`document.delete`), one for the confirmed cascade completion (`data.deletion_confirmed`).

---

## Architecture — SOLID & DRY

### File layout

```
src/domain/
    deletion_service.py    # DeletionService class (this task)
    schemas.py             # DeletionReport, DeletionContext
```

### DeletionReport schema

```python
from pydantic import BaseModel, Field
from uuid import UUID

class DeletionError(BaseModel):
    resource_type: str    # "qdrant_points", "minio_object", "chunks_registry"
    resource_id: str      # document_id or object key
    error: str            # error message (no PII, no content)
    retries: int

class DeletionReport(BaseModel):
    deleted_documents: int = 0
    deleted_chunks: int = 0              # rows removed from chunks_registry
    deleted_qdrant_points: int = 0       # approximate count from Qdrant
    deleted_minio_objects: int = 0
    failed_documents: int = 0            # documents that hit deletion_failed
    errors: list[DeletionError] = Field(default_factory=list)

    def merge(self, other: "DeletionReport") -> "DeletionReport":
        """Aggregate reports when iterating documents in a collection deletion."""
        return DeletionReport(
            deleted_documents=self.deleted_documents + other.deleted_documents,
            deleted_chunks=self.deleted_chunks + other.deleted_chunks,
            deleted_qdrant_points=self.deleted_qdrant_points + other.deleted_qdrant_points,
            deleted_minio_objects=self.deleted_minio_objects + other.deleted_minio_objects,
            failed_documents=self.failed_documents + other.failed_documents,
            errors=self.errors + other.errors,
        )
```

### DeletionService class

```python
import structlog
from uuid import UUID
from src.domain.schemas import DeletionReport, DeletionError
from src.retrieval.service import RetrievalService
from src.retrieval.schemas import TenantContext
from src.core.clients.minio import MinIOClient
from src.core.exceptions import DeletionError as DeletionException

log = structlog.get_logger(__name__)

class DeletionService:
    """GDPR Art. 17 orchestrator. Single entry point for all cascading deletions.

    Operation order follows docs/architecture.md §5:
    Postgres (status update) → Qdrant → MinIO → Postgres (cleanup + audit).

    Any new data store added to the platform must be registered here.
    """

    def __init__(
        self,
        db: AsyncSession,
        retrieval: RetrievalService,
        minio: MinIOClient,
    ) -> None:
        self._db = db
        self._retrieval = retrieval
        self._minio = minio

    async def delete_document(
        self,
        ctx: TenantContext,
        document_id: UUID,
    ) -> DeletionReport:
        ...

    async def delete_collection(
        self,
        ctx: TenantContext,
        collection_id: UUID,
    ) -> DeletionReport:
        ...

    async def delete_tenant(
        self,
        ctx: TenantContext,
        tenant_id: UUID,
    ) -> DeletionReport:
        ...
```

---

## Implementation Steps

### Step 1: `delete_document()` method

```python
async def delete_document(
    self,
    ctx: TenantContext,
    document_id: UUID,
) -> DeletionReport:
    """Cascade-delete a document across Postgres, Qdrant, and MinIO.

    Order: Postgres status=deleted → Qdrant points → MinIO objects →
           chunks_registry cleanup → audit_log confirmation.

    On partial failure: document.status set to deletion_failed; error logged
    and appended to DeletionReport.errors; never silent.

    Args:
        ctx: Tenant context. document must belong to ctx.tenant_id.
        document_id: Document to delete.

    Returns:
        DeletionReport with counts of deleted resources and any errors.

    Raises:
        DocumentNotFoundError: If document does not exist or belongs to different tenant.
    """
    report = DeletionReport()

    # --- Pre-flight: load document, verify tenant ownership ---
    doc = await self._db.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.tenant_id == ctx.tenant_id,    # resource-level auth
        )
    )
    if not doc:
        raise DocumentNotFoundError(f"document {document_id} not found for tenant {ctx.tenant_id}")
    if doc.status == "deleted":
        # Idempotent: already deleted, return success
        return report

    # Resolve Qdrant collection name from the document's collection → embedding model
    qdrant_collection = await self._resolve_qdrant_collection(doc.collection_id)

    # --- Step 1: Mark deleted in Postgres (immediately stops retrieval) ---
    await self._db.execute(
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == ctx.tenant_id)
        .values(status="deleted")
    )
    await self._db.flush()

    # Audit log: deletion initiated
    await self._insert_audit_log(
        tenant_id=ctx.tenant_id,
        action="document.delete",
        resource_type="document",
        resource_id=document_id,
        details={"initiator": "api"},
    )

    # --- Step 2: Delete Qdrant points ---
    qdrant_deleted = 0
    try:
        qdrant_deleted = await self._delete_qdrant_with_retry(
            ctx=ctx,
            qdrant_collection=qdrant_collection,
            document_id=document_id,
        )
    except Exception as exc:
        await self._mark_deletion_failed(document_id, ctx.tenant_id)
        report.errors.append(DeletionError(
            resource_type="qdrant_points",
            resource_id=str(document_id),
            error=str(exc),
            retries=3,
        ))
        report.failed_documents += 1
        log.error("deletion.qdrant_failed", document_id=str(document_id), error=str(exc))
        return report   # abort further steps; state is deletion_failed

    report.deleted_qdrant_points += qdrant_deleted

    # --- Step 3: Delete MinIO objects (raw/ and processed/ paths) ---
    minio_deleted = 0
    try:
        minio_deleted = await self._delete_minio_with_retry(doc)
        # Null out the minio_key so no future presigned URL is possible
        await self._db.execute(
            update(Document)
            .where(Document.id == document_id, Document.tenant_id == ctx.tenant_id)
            .values(minio_key=None)
        )
    except Exception as exc:
        await self._mark_deletion_failed(document_id, ctx.tenant_id)
        report.errors.append(DeletionError(
            resource_type="minio_object",
            resource_id=doc.minio_key or "unknown",
            error=str(exc),
            retries=3,
        ))
        report.failed_documents += 1
        log.error("deletion.minio_failed", document_id=str(document_id), error=str(exc))
        return report

    report.deleted_minio_objects += minio_deleted

    # --- Step 4: Delete chunks_registry rows ---
    result = await self._db.execute(
        delete(ChunksRegistry)
        .where(
            ChunksRegistry.document_id == document_id,
            ChunksRegistry.tenant_id == ctx.tenant_id,
        )
    )
    chunks_deleted = result.rowcount
    report.deleted_chunks += chunks_deleted

    # --- Step 5: Audit log confirmation ---
    await self._insert_audit_log(
        tenant_id=ctx.tenant_id,
        action="data.deletion_confirmed",
        resource_type="document",
        resource_id=document_id,
        details={
            "qdrant_points": qdrant_deleted,
            "minio_objects": minio_deleted,
            "chunks_registry": chunks_deleted,
        },
    )

    await self._db.commit()
    report.deleted_documents += 1
    log.info("deletion.document_complete", document_id=str(document_id),
             qdrant_points=qdrant_deleted, minio_objects=minio_deleted)
    return report
```

### Step 2: `_delete_qdrant_with_retry()` helper

```python
from tenacity import retry, stop_after_attempt, wait_exponential, add_jitter

async def _delete_qdrant_with_retry(
    self,
    ctx: TenantContext,
    qdrant_collection: str,
    document_id: UUID,
) -> int:
    """Retry Qdrant deletion up to 3 times with exponential backoff.

    Per docs/architecture.md §16: Qdrant upsert (used as reference for delete):
    3 attempts, exp backoff 1–10s, ±20% jitter.
    """
    @retry(
        stop=stop_after_attempt(3),
        wait=add_jitter(wait_exponential(multiplier=1, min=1, max=10), 0.2),
        reraise=True,
    )
    async def _attempt():
        return await self._retrieval.delete_by_document(
            ctx=ctx,
            qdrant_collection=qdrant_collection,
            document_id=document_id,
        )

    return await _attempt()
```

### Step 3: `_delete_minio_with_retry()` helper

```python
async def _delete_minio_with_retry(self, doc: Document) -> int:
    """Delete raw/ and processed/ objects for a document from MinIO.

    Per docs/architecture.md §16: MinIO write: 3 attempts, exp backoff 2–20s, ±20% jitter.
    Returns count of deleted objects (0, 1, or 2 depending on processed/ existence).
    """
    @retry(
        stop=stop_after_attempt(3),
        wait=add_jitter(wait_exponential(multiplier=2, min=2, max=20), 0.2),
        reraise=True,
    )
    async def _attempt():
        # BLOCKER FIX: Read minio_key and minio_bucket directly from the Document model.
        # Never reconstruct the bucket name from doc.tenant_slug — that attribute does not
        # exist on Document and would cause AttributeError at runtime.
        # minio_key and minio_bucket are set at upload time in TASK-006.
        #
        # If Document does not yet have a minio_bucket column, derive bucket as:
        #   f"tenant-{ctx.tenant_id}"
        # and read that from the outer scope (ctx must be captured or passed explicitly).
        bucket = doc.minio_bucket if hasattr(doc, "minio_bucket") else f"tenant-{doc.tenant_id}"
        deleted = 0

        # Delete raw/ object (the original uploaded file) using the stored key.
        if doc.minio_key:
            await self._minio.delete_object(bucket=bucket, key=doc.minio_key)
            deleted += 1

        # Delete processed/ extraction output (may not exist if extraction failed).
        # Key pattern: processed/{doc.id}/extracted.json — derived, not stored.
        processed_key = f"processed/{doc.id}/extracted.json"
        try:
            await self._minio.delete_object(bucket=bucket, key=processed_key)
            deleted += 1
        except ObjectNotFoundError:
            pass   # processed/ may not exist for failed documents

        return deleted

    return await _attempt()
```

### Step 4: `delete_collection()` method

```python
async def delete_collection(
    self,
    ctx: TenantContext,
    collection_id: UUID,
) -> DeletionReport:
    """Delete a collection and all its documents.

    Iterates all documents in the collection and calls delete_document for each.
    After all documents are deleted, removes the collection row from Postgres.
    Audit log is written per-document and once for the collection.

    Args:
        ctx: Tenant context. Collection must belong to ctx.tenant_id.
        collection_id: Collection to delete.

    Returns:
        Aggregated DeletionReport for all documents in the collection.
    """
    # Verify collection ownership
    collection = await self._db.scalar(
        select(Collection)
        .where(
            Collection.id == collection_id,
            Collection.tenant_id == ctx.tenant_id,
        )
    )
    if not collection:
        raise CollectionNotFoundError(f"collection {collection_id} not found")

    # Load all documents (non-deleted) in the collection
    doc_ids = await self._db.scalars(
        select(Document.id)
        .where(
            Document.collection_id == collection_id,
            Document.tenant_id == ctx.tenant_id,
            Document.status != "deleted",
        )
    )

    report = DeletionReport()
    for doc_id in doc_ids:
        doc_report = await self.delete_document(ctx=ctx, document_id=doc_id)
        report = report.merge(doc_report)

    # Any documents that failed deletion stop the collection deletion
    if report.failed_documents > 0:
        log.error("deletion.collection_partial_failure",
                  collection_id=str(collection_id),
                  failed=report.failed_documents)
        await self._insert_audit_log(
            tenant_id=ctx.tenant_id,
            action="collection.deletion_partial_failure",
            resource_type="collection",
            resource_id=collection_id,
            details={"failed_documents": report.failed_documents,
                     "errors": len(report.errors)},
        )
        return report   # collection row NOT deleted; state is partially cleaned up

    # Delete collection row from Postgres
    await self._db.execute(
        delete(Collection)
        .where(Collection.id == collection_id, Collection.tenant_id == ctx.tenant_id)
    )

    await self._insert_audit_log(
        tenant_id=ctx.tenant_id,
        action="collection.deleted",
        resource_type="collection",
        resource_id=collection_id,
        details={"deleted_documents": report.deleted_documents,
                 "deleted_chunks": report.deleted_chunks},
    )
    await self._db.commit()
    return report
```

### Step 5: `delete_tenant()` method

This is a destructive, irreversible system-level operation. It must only be callable by users with the `admin:tenant_delete` permission (Owner role) or by internal system processes.

```python
async def delete_tenant(
    self,
    ctx: TenantContext,
    tenant_id: UUID,
) -> DeletionReport:
    """Full tenant offboarding: delete all collections, users links, and tenant row.

    This is an Owner-only or system-level operation.
    The caller must verify permission before invoking this method.

    Order:
    1. Iterate all collections → delete_collection() for each
    2. Delete all remaining Qdrant points with tenant filter (cleanup safety net)
    3. Delete user_roles for this tenant
    4. Delete user_tenants for this tenant
    5. Set tenant.status = deleted
    6. Audit log

    The MinIO bucket is NOT deleted by this service — that is an infrastructure
    operation (manual or via admin tooling) to avoid accidental data loss on misconfiguration.

    Args:
        ctx: Tenant context. tenant_id must equal ctx.tenant_id (no cross-tenant ops).
        tenant_id: Tenant to delete. Must match ctx.tenant_id.

    Returns:
        Aggregated DeletionReport.
    """
    if tenant_id != ctx.tenant_id:
        raise ValueError("delete_tenant: tenant_id must match ctx.tenant_id")

    log.warning("deletion.tenant_initiated", tenant_id=str(tenant_id))

    # Verify tenant exists
    tenant = await self._db.get(Tenant, tenant_id)
    if not tenant or tenant.status == "deleted":
        raise TenantNotFoundError(f"tenant {tenant_id} not found")

    # Step 1: Delete all collections
    collection_ids = await self._db.scalars(
        select(Collection.id)
        .where(Collection.tenant_id == tenant_id)
    )
    report = DeletionReport()
    for cid in collection_ids:
        col_report = await self.delete_collection(
            ctx=ctx,
            collection_id=cid,
        )
        report = report.merge(col_report)

    # Step 2: Qdrant safety net — delete any remaining points (e.g., from failed document deletes)
    # Get all Qdrant collections that could contain this tenant's data
    qdrant_collections = await self._get_all_qdrant_collections()
    for qc in qdrant_collections:
        try:
            extra_deleted = await self._retrieval.delete_by_tenant(ctx=ctx, qdrant_collection=qc)
            report.deleted_qdrant_points += extra_deleted
        except Exception as exc:
            report.errors.append(DeletionError(
                resource_type="qdrant_tenant_cleanup",
                resource_id=str(tenant_id),
                error=str(exc),
                retries=0,
            ))

    # Step 3: Remove role assignments
    await self._db.execute(
        delete(UserRole)
        .where(UserRole.role_id.in_(
            select(Role.id).where(Role.tenant_id == tenant_id)
        ))
    )

    # Step 4: Remove tenant memberships
    await self._db.execute(
        delete(UserTenant).where(UserTenant.tenant_id == tenant_id)
    )

    # Step 5: Soft-delete tenant
    await self._db.execute(
        update(Tenant)
        .where(Tenant.id == tenant_id)
        .values(status="deleted")
    )

    # Step 6: Final audit log
    await self._insert_audit_log(
        tenant_id=tenant_id,
        action="tenant.deleted",
        resource_type="tenant",
        resource_id=tenant_id,
        details={
            "deleted_documents": report.deleted_documents,
            "deleted_chunks": report.deleted_chunks,
            "deleted_qdrant_points": report.deleted_qdrant_points,
            "failed_documents": report.failed_documents,
        },
    )

    await self._db.commit()
    log.warning("deletion.tenant_complete", tenant_id=str(tenant_id),
                deleted_docs=report.deleted_documents)
    return report
```

### Step 6: Private helpers

```python
async def _mark_deletion_failed(self, document_id: UUID, tenant_id: UUID) -> None:
    """Mark a document as deletion_failed after exhausting retries.

    The document is NOT removed from Postgres — it stays in deletion_failed status
    so an admin can retry or investigate.
    """
    await self._db.execute(
        update(Document)
        .where(Document.id == document_id, Document.tenant_id == tenant_id)
        .values(status="deletion_failed")
    )
    await self._db.flush()


async def _resolve_qdrant_collection(self, collection_id: UUID) -> str:
    """Resolve the Qdrant collection name for a document's collection.

    Pattern: emb_{embedding_model_slug} — consistent with PipelineConfig.qdrant_collection
    naming used throughout the codebase. Do NOT use chunks__ prefix.
    """
    model = await self._db.scalar(
        select(ModelsRegistry.name)
        .join(Collection, Collection.embedding_model_id == ModelsRegistry.id)
        .where(Collection.id == collection_id)
    )
    return f"emb_{slugify(model)}"


async def _insert_audit_log(
    self,
    tenant_id: UUID,
    action: str,
    resource_type: str,     # MAJOR FIX: explicit parameter; never derived by string inspection
    resource_id: UUID,
    details: dict,
) -> None:
    """Insert an audit log entry. details must NOT contain PII or document content.

    Args:
        resource_type: Explicit resource type string (e.g., "document", "collection",
            "tenant", "tos_version"). Do NOT infer from action string — callers must
            pass this explicitly.
    """
    self._db.add(AuditLog(
        tenant_id=tenant_id,
        user_id=None,               # system action
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    ))
    await self._db.flush()


async def _get_all_qdrant_collections(self) -> list[str]:
    """Get all physical Qdrant collection names (for tenant-wide cleanup)."""
    models = await self._db.scalars(
        select(ModelsRegistry.name)
        .where(
            ModelsRegistry.type == "embedding",
            ModelsRegistry.is_active == True,
        )
    )
    return [f"emb_{slugify(name)}" for name in models]
```

### Step 7: Register in FastAPI dependency

```python
# src/api/dependencies/deletion.py
from src.domain.deletion_service import DeletionService
from src.api.dependencies.retrieval import get_retrieval_service
from src.core.clients.minio import get_minio_client

async def get_deletion_service(
    db: AsyncSession = Depends(get_db),
    retrieval: RetrievalService = Depends(get_retrieval_service),
    minio: MinIOClient = Depends(get_minio_client),
) -> DeletionService:
    return DeletionService(db=db, retrieval=retrieval, minio=minio)
```

Called from:

```python
# src/api/routers/documents.py
@router.delete("/{document_id}", status_code=202)
async def delete_document(
    document_id: UUID,
    user_ctx: UserContext = Depends(require("documents:delete")),
    deletion_svc: DeletionService = Depends(get_deletion_service),
    db: AsyncSession = Depends(get_db),
):
    """Initiate cascading document deletion. Returns 202 (deletion is async-ish)."""
    doc = await db.get(Document, document_id)
    if not doc or doc.tenant_id != user_ctx.tenant_id:
        raise HTTPException(404)
    # Permission refinement: documents:delete_own means only own documents
    if "documents:delete" not in user_ctx.permissions:
        if "documents:delete_own" in user_ctx.permissions and doc.uploaded_by != user_ctx.user_id:
            raise HTTPException(403, {"code": "PERMISSION_DENIED"})

    report = await deletion_svc.delete_document(
        ctx=TenantContext(tenant_id=user_ctx.tenant_id, allowed_collection_ids=[]),
        document_id=document_id,
    )
    return {"status": "deleted", "report": report.model_dump()}
```

---

## API Contracts

`DeletionService` does not expose HTTP endpoints directly — it is called by routers. The following endpoints use it:

| HTTP Endpoint | DeletionService method | Permission |
|---|---|---|
| `DELETE /documents/{id}` | `delete_document()` | `documents:delete` or `documents:delete_own` |
| `DELETE /collections/{id}` | `delete_collection()` | `admin:collections` |
| `DELETE /tenants/{id}` | `delete_tenant()` | Owner role or system |

### DeletionReport API response

```json
{
  "deleted_documents": 1,
  "deleted_chunks": 47,
  "deleted_qdrant_points": 47,
  "deleted_minio_objects": 2,
  "failed_documents": 0,
  "errors": []
}
```

On partial failure:

```json
{
  "deleted_documents": 0,
  "deleted_chunks": 0,
  "deleted_qdrant_points": 0,
  "deleted_minio_objects": 0,
  "failed_documents": 1,
  "errors": [
    {
      "resource_type": "qdrant_points",
      "resource_id": "doc-uuid",
      "error": "connection refused",
      "retries": 3
    }
  ]
}
```

---

## Security Checklist

- [ ] `delete_document()` verifies `document.tenant_id == ctx.tenant_id` before any deletion. Cross-tenant deletion attempt → `DocumentNotFoundError` (not 403 — avoids leaking existence).
- [ ] `delete_collection()` verifies `collection.tenant_id == ctx.tenant_id`.
- [ ] `delete_tenant()` verifies `tenant_id == ctx.tenant_id` — a service cannot delete another tenant's data even if called programmatically.
- [ ] `_mark_deletion_failed()` does NOT expose which specific step failed in any HTTP response — only in `DeletionReport.errors` which is visible to admins only.
- [ ] Audit log is written for every deletion operation, even partial failures.
- [ ] `audit_log.details` contains only counts and IDs — no document content, no chunk text, no PII.
- [ ] MinIO bucket is NOT deleted by `delete_tenant()` — this is intentional to prevent accidental data loss. The bucket must be deleted manually by an operator after confirming all data is cleared.
- [ ] `DeletionService` does not check permissions — callers (routers) are responsible for permission checks before calling the service. This follows the layered architecture rule.
- [ ] `delete_by_document()` in `RetrievalService` is called with both `tenant_id` AND `document_id` filters — confirmed in TASK-009 security checklist.
- [ ] On Qdrant failure: status → `deletion_failed`; MinIO deletion is NOT attempted (Qdrant failure aborts the cascade to prevent orphaned MinIO objects with no Postgres record).

---

## Terms of Use (relevant constraints)

- **Deletion is cascading and irreversible**: once `delete_document()` returns successfully, the document cannot be recovered. The MinIO object is gone. The Postgres row remains in `deleted` status for audit purposes only.
- **`deletion_failed` status**: documents stuck in `deletion_failed` can be retried by calling `delete_document()` again — the method is idempotent (it detects `deleted` status and returns early; it detects `deletion_failed` and re-attempts). Re-attempting a `deletion_failed` document is safe.
- **`message_sources` preservation**: when a document is deleted, `message_sources.document_id` becomes NULL and `message_sources.chunk_id` becomes NULL (via ON DELETE SET NULL). The citation record remains for audit — the UI shows "source document deleted". This is intentional per GDPR balance: deletion of the source does not require deletion of the fact that a citation was made.
- **New data stores**: any future data store (e.g., a BM25 index, a full-text search index) MUST be added to `DeletionService` as a new deletion step. Document this requirement in the relevant task's Definition of Done.
- **MinIO object locking**: if MinIO versioning is enabled (per `docs/data-model.md` §4, it is), deletion removes the current version. Object Lock (WORM) is NOT enabled — we need deletions to succeed.

---

## Tests

### Unit tests (`tests/unit/domain/test_deletion_service.py`)

All external clients mocked: `AsyncMock` for `RetrievalService`, `MinIOClient`, `AsyncSession`.

**Document deletion tests:**

| Test | Setup | Assert |
|---|---|---|
| `test_delete_document_happy_path` | Normal document; all mocks succeed | `report.deleted_documents == 1`; `report.errors == []` |
| `test_delete_document_sets_status_deleted` | Normal flow | DB update called with `status="deleted"` |
| `test_delete_document_calls_qdrant` | Normal flow | `retrieval.delete_by_document` called with correct `document_id` and `ctx.tenant_id` |
| `test_delete_document_nulls_minio_key` | Normal flow | DB update called with `minio_key=None` |
| `test_delete_document_writes_audit_log` | Normal flow | Two audit log inserts: `document.delete` and `data.deletion_confirmed` |
| `test_delete_document_qdrant_failure_sets_deletion_failed` | Qdrant mock raises after 3 retries | `document.status = deletion_failed`; `report.failed_documents == 1`; `report.errors` has Qdrant entry |
| `test_delete_document_minio_failure_sets_deletion_failed` | MinIO mock raises after 3 retries | `deletion_failed`; `report.errors` has MinIO entry |
| `test_delete_document_minio_not_called_after_qdrant_failure` | Qdrant fails | MinIO client never called (cascade aborted) |
| `test_delete_document_idempotent_already_deleted` | `document.status == "deleted"` | Returns empty report immediately; no external calls |
| `test_delete_document_cross_tenant_raises` | Document belongs to different tenant | Raises `DocumentNotFoundError` before any deletion |
| `test_delete_document_chunks_registry_cleaned` | 5 chunks in DB | DB delete called for all 5 `chunks_registry` rows |

**Collection deletion tests:**

| Test | Setup | Assert |
|---|---|---|
| `test_delete_collection_iterates_all_documents` | Collection with 3 docs | `delete_document` called 3 times |
| `test_delete_collection_deletes_collection_row` | All docs deleted successfully | `Collection` row deleted from DB |
| `test_delete_collection_aborts_on_partial_failure` | 1 of 3 docs fails | Collection row NOT deleted; `report.failed_documents == 1` |

**Tenant deletion tests:**

| Test | Setup | Assert |
|---|---|---|
| `test_delete_tenant_iterates_all_collections` | Tenant with 2 collections | `delete_collection` called twice |
| `test_delete_tenant_sets_tenant_deleted` | Normal flow | Tenant `status = "deleted"` |
| `test_delete_tenant_removes_user_roles` | Tenant has 3 role assignments | `UserRole` rows deleted |
| `test_delete_tenant_removes_user_tenants` | Tenant has 2 user memberships | `UserTenant` rows deleted |
| `test_delete_tenant_cross_tenant_raises` | `tenant_id != ctx.tenant_id` | Raises `ValueError` before any deletion |
| `test_delete_tenant_runs_qdrant_safety_net` | After collection deletion | `retrieval.delete_by_tenant` called for each Qdrant collection |

### Integration tests (`tests/integration/test_deletion_cascade.py`)

Use testcontainers for Postgres, Qdrant, MinIO:

| Test | Assert |
|---|---|
| `test_delete_document_end_to_end` | After deletion: Qdrant search returns 0 hits; MinIO object absent; chunks_registry empty; message_sources.document_id is NULL |
| `test_delete_collection_removes_all_chunks` | All chunks for all docs in collection deleted from Qdrant |
| `test_deletion_failed_can_be_retried` | First call: Qdrant down → `deletion_failed`; Second call: Qdrant up → `deleted` and complete |
| `test_message_sources_preserved_after_document_delete` | Message sources with NULL document_id remain; not deleted |

---

## Definition of Done

- [ ] `DeletionService` implements `delete_document`, `delete_collection`, `delete_tenant` with correct cascade order.
- [ ] `DeletionReport` Pydantic model with all required fields.
- [ ] Qdrant and MinIO steps wrapped with tenacity retry (3 attempts per §16).
- [ ] On Qdrant failure: document set to `deletion_failed`; MinIO NOT touched; error in report.
- [ ] Two audit log entries per successful document deletion: `document.delete` + `data.deletion_confirmed`.
- [ ] `delete_document()` is idempotent: calling on an already-deleted document returns empty report.
- [ ] `delete_tenant()` verifies `tenant_id == ctx.tenant_id` before starting.
- [ ] All unit tests pass: `pytest tests/unit/domain/test_deletion_service.py -x -q`.
- [ ] Integration tests with testcontainers pass.
- [ ] `mypy src/domain/deletion_service.py` passes.
- [ ] `ruff check src/domain/deletion_service.py` passes.
- [ ] GDPR audit log entries contain no PII or document content — verified by unit tests.
