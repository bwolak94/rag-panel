# TASK-006: Document Upload API

**Status:** TODO
**Priority:** P1 — required before ingest worker (TASK-007)
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile), security-auditor
**Related docs:** `docs/architecture.md` §5, §15 | `docs/data-model.md` §2.2 (documents, ingestion_jobs)
**Estimated effort:** 3–4 days

---

## Overview

Implement the document upload flow using MinIO presigned URLs. The flow is: client calls `POST /documents` → API validates, creates DB record, returns a presigned `PUT` URL (TTL ≤ 5 minutes) → client uploads directly to MinIO → MinIO fires `ObjectCreated:Put` webhook → webhook handler enriches the event with `tenant_id` and `document_id` (parsed from the object key path) → enriched event is published to Redis Streams `ingest_events`.

This design decouples the upload (synchronous, client-facing) from processing (asynchronous, worker). The API never proxies binary file content through itself — MinIO presigned URLs handle the byte transfer directly.

MIME validation, size limit (100 MB), filename sanitization, and SHA-256 dedup (409 Conflict on duplicate within tenant) are enforced at the API layer before the presigned URL is issued.

## Usage

**Who calls `POST /documents`:** Authenticated users with `documents:upload` permission (Contributor+).

**Flow summary:**

```
Client → POST /documents (metadata + sha256) → API validates → DB INSERT (status=uploaded)
       ← 202 Accepted { document_id, upload_url (presigned PUT), expires_at }

Client → PUT {upload_url} (binary file body)
       → MinIO stores at raw/{collection_id}/{document_id}/{filename}
       → MinIO fires ObjectCreated:Put webhook
       → Webhook handler → Redis Streams ingest_events

Ingest worker consumes event (TASK-007)
```

**Why presigned URL instead of multipart upload through API:**
- API does not buffer large files in memory or disk
- Eliminates API as upload bandwidth bottleneck
- MinIO handles resume-on-failure at the object storage level

## Tech Stack

- **FastAPI** — `APIRouter(prefix="/documents")`
- **minio-py 7.2+** — presigned URL generation; sync client wrapped in `asyncio.run_in_executor`
- **redis-py 5.2+** with `aioredis`-compatible async client — `XADD` to `ingest_events`
- **python-magic** — MIME type detection from file header bytes (not filename extension), installed as `python-magic-bin` on Windows
- **hashlib** — SHA-256 digest computation (client-provided; server verifies on webhook)
- **SQLAlchemy 2.x** — `Document`, `IngestionJob` insert
- **Pydantic v2** — request/response schemas with MIME and size validation
- TASK-003 auth dependencies

## Database Patterns

### Document Insert Sequence

```
1. Validate MIME type ∈ ALLOWED_MIME_TYPES
2. Validate size_bytes ≤ 100 * 1024 * 1024
3. Sanitize filename
4. Check (tenant_id, sha256) uniqueness — return 409 if duplicate
5. INSERT documents (status='uploaded', minio_key computed)
6. INSERT ingestion_jobs (status='pending', document_id=<new>)
7. Generate presigned PUT URL (TTL = INGEST_PRESIGNED_URL_TTL_SECONDS)
8. Return 202 with {document_id, job_id, upload_url, expires_at}
9. session.commit()
```

Note: MinIO URL is generated BEFORE commit. If commit fails, the presigned URL is valid but no DB record exists — the MinIO webhook will attempt to publish but the webhook handler will fail to find the document and will drop the event (idempotent: no orphan processing). The client should retry `POST /documents` in this case.

```python
# src/db/repositories/document_repository.py
import uuid
import hashlib
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import Document, IngestionJob
from src.core.exceptions import ConflictError


ALLOWED_MIME_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",       # xlsx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",# pptx
    "text/html",
    "text/markdown",
    "text/plain",
})

MAX_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check_duplicate(
        self, tenant_id: uuid.UUID, sha256: str
    ) -> Document | None:
        """Returns existing document if SHA-256 already exists for this tenant."""
        q = select(Document).where(
            Document.tenant_id == tenant_id,
            Document.sha256 == sha256,
            Document.status != "deleted",
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def create_upload_record(
        self,
        *,
        tenant_id: uuid.UUID,
        collection_id: uuid.UUID,
        title: str,
        original_filename: str,
        minio_key: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        uploaded_by: uuid.UUID,
        tags: list[str],
    ) -> tuple[Document, IngestionJob]:
        doc = Document(
            tenant_id=tenant_id,
            collection_id=collection_id,
            title=title,
            original_filename=original_filename,
            minio_key=minio_key,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            status="uploaded",
            uploaded_by=uploaded_by,
            tags=tags,
        )
        self._session.add(doc)
        await self._session.flush()  # Get doc.id

        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=doc.id,
            status="pending",
            steps=[],
        )
        self._session.add(job)
        await self._session.flush()
        return doc, job

    async def get_by_id(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> Document | None:
        q = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def update_status(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID, status: str
    ) -> None:
        from sqlalchemy import update
        await self._session.execute(
            update(Document)
            .where(Document.id == document_id, Document.tenant_id == tenant_id)
            .values(status=status)
        )
        await self._session.flush()
```

## API Contracts

### Schemas

```python
# src/api/schemas/document.py
from __future__ import annotations
import re
import uuid
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator


# Canonical document status values
DocumentStatus = Literal[
    "uploaded", "validating", "needs_review", "indexing",
    "ready", "rejected", "failed", "deleted"
]


class DocumentUploadRequest(BaseModel):
    """
    Client-provided metadata before upload. Client computes SHA-256 locally
    so the API can deduplicate before the actual file transfer.
    """
    collection_id: uuid.UUID
    filename: str = Field(..., min_length=1, max_length=500)
    mime_type: str = Field(..., max_length=100)
    size_bytes: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    title: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        """Remove path traversal characters, null bytes, and reserved names."""
        # Strip directory components
        filename = v.split("/")[-1].split("\\")[-1]
        # Remove null bytes and control characters
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
        # Replace characters unsafe in object storage paths
        filename = re.sub(r'[<>:"|?*]', "_", filename)
        if not filename or filename in (".", ".."):
            raise ValueError("Invalid filename")
        return filename

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, v: str) -> str:
        from src.db.repositories.document_repository import ALLOWED_MIME_TYPES
        if v not in ALLOWED_MIME_TYPES:
            raise ValueError(
                f"MIME type '{v}' is not allowed. Allowed: {sorted(ALLOWED_MIME_TYPES)}"
            )
        return v

    @field_validator("size_bytes")
    @classmethod
    def validate_size(cls, v: int) -> int:
        from src.db.repositories.document_repository import MAX_SIZE_BYTES
        if v > MAX_SIZE_BYTES:
            raise ValueError(f"File size exceeds maximum of {MAX_SIZE_BYTES // 1024**2} MB")
        return v


class DocumentUploadResponse(BaseModel):
    """Returned to client; client uses upload_url for direct PUT to MinIO."""
    document_id: uuid.UUID
    job_id: uuid.UUID
    upload_url: str  # Presigned PUT URL
    minio_key: str   # Object path (informational)
    expires_at: datetime

    model_config = {"from_attributes": True}


class DocumentResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    category: str | None
    tags: list[str]
    language: str | None
    uploaded_by: uuid.UUID | None
    validation_result: dict | None  # Returned only to admins with documents:manage
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class MinIOWebhookEvent(BaseModel):
    """
    Schema for MinIO bucket notification. MinIO sends this as POST JSON.
    See docs/architecture.md §15 for canonical event schema.
    """
    EventName: str   # e.g., "s3:ObjectCreated:Put"
    Key: str         # e.g., "tenant-clinic/raw/coll-id/doc-id/file.pdf"
    Records: list[dict]  # Raw S3-compatible notification records
```

### Endpoints

```
POST   /documents              → 202 DocumentUploadResponse
GET    /documents              → 200 DocumentListResponse
GET    /documents/{id}         → 200 DocumentResponse
DELETE /documents/{id}         → 204

POST   /internal/minio-webhook → 200 (MinIO notification receiver)
```

#### `POST /documents`

**Permission:** `documents:upload`

```python
@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentUploadResponse)
async def initiate_upload(
    body: DocumentUploadRequest,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(require_permission("documents:upload"))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    request: Request,
) -> DocumentUploadResponse:
    from src.domain.document_service import DocumentService
    svc = DocumentService(session)
    return await svc.initiate_upload(body=body, ctx=ctx, client_ip=request.client.host)
```

**Full flow inside `DocumentService.initiate_upload()`:**

1. Verify collection_id is in `ctx.writable_collection_ids` (collection-level write check)
2. Check for SHA-256 duplicate: `DocumentRepository.check_duplicate(ctx.tenant_id, body.sha256)` → if found, return `409 Conflict` with `{"detail": "Duplicate document", "existing_document_id": "<uuid>"}`
3. Sanitize filename (already done by Pydantic validator)
4. Compute `minio_key = f"raw/{body.collection_id}/{<new_doc_id>}/{body.filename}"`
5. INSERT document and ingestion_job via `DocumentRepository.create_upload_record()`
6. Generate presigned PUT URL:
   ```python
   from datetime import timedelta
   from minio import Minio
   expiry = timedelta(seconds=settings.INGEST_PRESIGNED_URL_TTL_SECONDS)
   bucket = f"tenant-{tenant.slug}"
   url = minio_client.presigned_put_object(bucket, minio_key, expires=expiry)
   ```
7. Write audit log entry: `document.upload_initiated`
8. Commit session
9. Return `DocumentUploadResponse`

**Error responses:**

| Condition | Status | Body |
|---|---|---|
| Collection not in writable_collection_ids | 403 | `{"detail": "No write access to collection"}` |
| SHA-256 duplicate in tenant | 409 | `{"detail": "Duplicate document", "existing_document_id": "<uuid>"}` |
| MIME type not allowed | 422 | Pydantic validation error |
| File size > 100 MB | 422 | Pydantic validation error |
| Invalid filename | 422 | Pydantic validation error |

#### `POST /internal/minio-webhook`

**Authentication:** Internal endpoint — NOT authenticated via JWT. Protected by:
- Network: only reachable from `internal_net` (not routed through Traefik)
- Shared secret: `X-Minio-Webhook-Secret` header validated against `settings.MINIO_WEBHOOK_SECRET`

```python
# src/api/routers/webhooks.py
@webhook_router.post("/internal/minio-webhook", include_in_schema=False)
async def minio_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    """
    Receives MinIO ObjectCreated:Put notifications.
    Parses key path, enriches event, publishes to Redis Streams.
    See docs/architecture.md §15 for event schema.
    """
    # Validate shared secret
    secret = request.headers.get("X-Minio-Webhook-Secret", "")
    if not hmac.compare_digest(secret, settings.MINIO_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    await WebhookHandler(session).handle_minio_event(payload)
    return {"status": "accepted"}
```

### Webhook Handler

```python
# src/core/events/minio_webhook.py
import json
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.redis_client import get_redis_client
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger(__name__)


class WebhookHandler:
    STREAM_NAME = "ingest_events"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handle_minio_event(self, payload: dict) -> None:
        """
        Processes MinIO ObjectCreated:Put notification.
        Key path format: raw/{collection_id}/{document_id}/{filename}
        Bucket format: tenant-{slug}
        """
        for record in payload.get("Records", []):
            event_name: str = record.get("eventName", "")
            if "ObjectCreated" not in event_name:
                continue

            s3 = record.get("s3", {})
            bucket: str = s3.get("bucket", {}).get("name", "")
            key: str = s3.get("object", {}).get("key", "")
            size_bytes: int = s3.get("object", {}).get("size", 0)
            content_type: str = s3.get("object", {}).get("contentType", "")

            # Parse bucket → tenant slug → tenant_id
            if not bucket.startswith("tenant-"):
                logger.warning("webhook_unknown_bucket", bucket=bucket)
                continue
            slug = bucket.removeprefix("tenant-")

            tenant = await TenantRepository(self._session).get_by_slug(slug)
            if tenant is None:
                logger.error("webhook_tenant_not_found", slug=slug)
                continue

            # Parse key → collection_id, document_id
            # Expected: raw/{collection_id}/{document_id}/{filename}
            parts = key.split("/")
            if len(parts) < 4 or parts[0] != "raw":
                logger.warning("webhook_unexpected_key_format", key=key)
                continue

            try:
                collection_id = uuid.UUID(parts[1])
                document_id = uuid.UUID(parts[2])
            except ValueError:
                logger.error("webhook_invalid_ids_in_key", key=key)
                continue

            # Verify document exists in DB
            doc = await DocumentRepository(self._session).get_by_id(
                document_id, tenant.id
            )
            if doc is None:
                logger.error(
                    "webhook_document_not_found",
                    document_id=str(document_id),
                    tenant_id=str(tenant.id),
                )
                continue

            # Update document status to 'validating'
            await DocumentRepository(self._session).update_status(
                document_id, tenant.id, "validating"
            )
            await self._session.commit()

            # Publish enriched event to Redis Streams
            event = {
                "schema_version": "1",
                "event_type": "document.uploaded",
                "tenant_id": str(tenant.id),
                "document_id": str(document_id),
                "collection_id": str(collection_id),
                "minio_bucket": bucket,
                "minio_key": key,
                "size_bytes": str(size_bytes),
                "content_type": content_type,
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
            redis = await get_redis_client()
            await redis.xadd(self.STREAM_NAME, event)
            logger.info(
                "ingest_event_published",
                document_id=str(document_id),
                tenant_id=str(tenant.id),
            )
```

### Redis Client

```python
# src/core/clients/redis_client.py
import redis.asyncio as aioredis
from src.core.config import settings

_redis: aioredis.Redis | None = None


async def get_redis_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            str(settings.REDIS_URL),
            socket_connect_timeout=2,
            socket_timeout=6,
            retry_on_timeout=True,
            health_check_interval=30,
        )
    return _redis
```

## Architecture — SOLID & DRY

### Document Service

```python
# src/domain/document_service.py
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.auth import UserContext
from src.api.schemas.document import DocumentUploadRequest, DocumentUploadResponse
from src.core.clients.minio_client import get_minio_client
from src.core.config import settings
from src.core.exceptions import ConflictError, PermissionDeniedError
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService

logger = structlog.get_logger(__name__)


class DocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = DocumentRepository(session)
        self._tenant_repo = TenantRepository(session)
        self._audit = AuditService(session)
        self._session = session

    async def initiate_upload(
        self,
        body: DocumentUploadRequest,
        ctx: UserContext,
        client_ip: str | None,
    ) -> DocumentUploadResponse:
        # 1. Collection-level write access check
        if not ctx.can_write_collection(body.collection_id):
            raise PermissionDeniedError("No write access to this collection")

        # 2. SHA-256 dedup
        existing = await self._repo.check_duplicate(ctx.tenant_id, body.sha256)
        if existing is not None:
            raise ConflictError(
                f"Duplicate document. existing_document_id={existing.id}"
            )

        # 3. Compute MinIO key
        doc_id = uuid.uuid4()
        minio_key = f"raw/{body.collection_id}/{doc_id}/{body.filename}"

        # 4. Insert DB records
        doc, job = await self._repo.create_upload_record(
            tenant_id=ctx.tenant_id,
            collection_id=body.collection_id,
            title=body.title or body.filename,
            original_filename=body.filename,
            minio_key=minio_key,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            sha256=body.sha256,
            uploaded_by=ctx.user_id,
            tags=body.tags,
        )

        # 5. Generate presigned URL (sync MinIO call → run_in_executor)
        tenant = await self._tenant_repo.get_by_id(ctx.tenant_id)
        bucket = f"tenant-{tenant.slug}"
        ttl_seconds = settings.INGEST_PRESIGNED_URL_TTL_SECONDS
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

        import asyncio
        loop = asyncio.get_event_loop()
        upload_url: str = await loop.run_in_executor(
            None,
            lambda: get_minio_client().presigned_put_object(
                bucket, minio_key, expires=timedelta(seconds=ttl_seconds)
            ),
        )

        # 6. Audit
        await self._audit.log(
            ctx=ctx,
            action="document.upload_initiated",
            resource_type="document",
            resource_id=doc.id,
            details={
                "collection_id": str(body.collection_id),
                "mime_type": body.mime_type,
                "size_bytes": body.size_bytes,
            },
            ip=client_ip,
        )

        await self._session.commit()
        logger.info(
            "upload_initiated",
            document_id=str(doc.id),
            tenant_id=str(ctx.tenant_id),
        )
        return DocumentUploadResponse(
            document_id=doc.id,
            job_id=job.id,
            upload_url=upload_url,
            minio_key=minio_key,
            expires_at=expires_at,
        )
```

### What NOT to Do

- Do NOT proxy file bytes through the FastAPI process — this defeats the presigned URL design
- Do NOT trust the `mime_type` from the client alone; when the file is available (post-upload), the ingest graph re-validates with `python-magic` against file headers
- Do NOT log `sha256` values in application logs — they could allow fingerprinting of sensitive documents
- Do NOT generate presigned URLs with TTL > 300 seconds (enforced by `Settings` validator in TASK-001)
- Do NOT use `uuid.UUID(parts[1])` without try/except in the webhook handler — malformed keys must be logged and skipped, not crash the handler

## Implementation Steps

1. **Create `src/api/schemas/document.py`** — all schemas with validators

2. **Create `src/db/repositories/document_repository.py`** — `DocumentRepository` with `check_duplicate`, `create_upload_record`, `get_by_id`, `update_status`

3. **Create `src/core/clients/redis_client.py`** — lazy singleton `get_redis_client()`

4. **Create `src/core/events/minio_webhook.py`** — `WebhookHandler` class

5. **Create `src/domain/document_service.py`** — `DocumentService.initiate_upload()`

6. **Create `src/api/routers/documents.py`** — `POST /documents`, `GET /documents`, `GET /documents/{id}`, `DELETE /documents/{id}`

7. **Create `src/api/routers/webhooks.py`** — `POST /internal/minio-webhook` with HMAC secret validation

8. **Add `MINIO_WEBHOOK_SECRET` to `Settings` and `.env.example`**

9. **Configure MinIO bucket notification** (documented in `docker-compose.yml` or startup script):
   ```shell
   mc alias set local http://minio:9000 minioadmin minioadmin
   mc admin config set local notify_webhook:1 endpoint="http://rag-api:8000/internal/minio-webhook" \
       auth_token="${MINIO_WEBHOOK_SECRET}"
   mc event add local/tenant-example s3:ObjectCreated:Put --prefix raw/ \
       --arn arn:minio:sqs::1:webhook
   ```

10. **Register routers in `src/main.py`**

11. **Update `docs/03-Specyfikacja-API.md`** with all document endpoints

12. **Write tests** (see Tests section)

13. **Run:** `ruff check --fix . && mypy src/ && pytest -x -q tests/unit/test_document_upload.py`

## Security Checklist

- Filename sanitization: strip directory separators (`/`, `\`), control characters, null bytes, reserved OS names
- MIME type validated against allowlist — extensions are not trusted
- SHA-256 validated as exactly 64 lowercase hex characters (`^[a-f0-9]{64}$`)
- Presigned URL TTL enforced ≤ 300 seconds via `Settings` validator
- Webhook endpoint not in `docs_url` (`include_in_schema=False`), not routed through Traefik
- Webhook HMAC secret validated with `hmac.compare_digest` (timing-safe comparison)
- Collection write access checked before DB insert: `ctx.can_write_collection(collection_id)`
- Tenant isolation: `minio_key` includes `collection_id` which belongs to tenant; bucket is `tenant-{slug}` (per-tenant bucket)
- SHA-256 not logged (document content fingerprint)
- `validation_result` JSONB not returned unless user has `documents:manage` permission
- Redis `XADD` publishes to `ingest_events` only after DB commit — no orphan events if DB fails
- `document_id` in webhook is verified against DB (`get_by_id`) before publishing to stream

## Terms of Use (relevant constraints)

- Medical data: uploaded files may contain patient records (GDPR Art. 9 special category). The API must not log file contents, filenames that may contain PII, or SHA-256 hashes.
- Allowed MIME types list is the hard boundary — extension-based checks are not a substitute
- Presigned URL TTL must never exceed 5 minutes (GDPR: minimize window of URL-based unauthorized access)
- Duplicate detection via SHA-256 is per-tenant — the same file may be uploaded to multiple tenants without conflict (cross-tenant SHA-256 uniqueness is NOT enforced)
- File size limit (100 MB) applies per file. No batch upload in MVP.

## Tests

References TASK-016 `tests/integration/test_api_contracts.py`.

**`tests/unit/test_filename_sanitization.py`**

- `"../../etc/passwd"` → `"passwd"` (directory traversal stripped)
- `"file\x00name.pdf"` → `"filename.pdf"` (null byte removed)
- `"."` → `ValueError`
- `".."` → `ValueError`
- `"file<script>.pdf"` → `"file_script_.pdf"`
- Normal filename `"my-document.pdf"` → unchanged

**`tests/unit/test_document_upload.py`**

- `POST /documents` valid body → 202 with `upload_url`, `document_id`, `job_id`
- `POST /documents` with MIME type `application/x-executable` → 422
- `POST /documents` with `size_bytes > 100MB` → 422
- `POST /documents` with invalid sha256 (wrong format) → 422
- `POST /documents` with duplicate sha256 in same tenant → 409 with `existing_document_id`
- Same sha256 in DIFFERENT tenant → 202 (cross-tenant duplicates allowed)
- `POST /documents` to collection not in `ctx.writable_collection_ids` → 403
- `POST /documents` as Viewer → 403
- Presigned URL TTL matches `settings.INGEST_PRESIGNED_URL_TTL_SECONDS`
- `IngestionJob` with status `pending` created alongside `Document`
- Audit log entry `document.upload_initiated` written

**`tests/unit/test_minio_webhook.py`**

- Valid MinIO event → document status updated to `validating`, event published to Redis
- Event with unknown bucket prefix → logged warning, no Redis publish
- Event with malformed key (not `raw/{uuid}/{uuid}/...`) → logged warning, skipped
- Event with unknown tenant slug → logged error, skipped
- Event with unknown document_id → logged error, skipped
- `X-Minio-Webhook-Secret` header missing → 403
- Wrong secret → 403
- `schema_version=1` present in Redis event

**`tests/security/test_idor.py`** (mark: `@pytest.mark.tenant_isolation`)

- `GET /documents/{doc_id_from_tenant_B}` as user of tenant A → 403
- `DELETE /documents/{doc_id_from_tenant_B}` as user of tenant A → 403
- Presigned URL from tenant A cannot be used to read tenant B's bucket (MinIO enforces this)

**Authorization matrix:**

| Endpoint | Admin | Contributor | Viewer |
|---|---|---|---|
| `POST /documents` | 202 | 202 | 403 |
| `GET /documents` | 200 | 200 | 200 |
| `GET /documents/{id}` | 200 | 200 | 200 |
| `DELETE /documents/{id}` | 204 | 403 | 403 |

## Definition of Done

- [ ] `POST /documents` returns 202 with presigned PUT URL (TTL ≤ 5 min)
- [ ] Filename sanitization removes path traversal, null bytes, control chars
- [ ] MIME type validated against allowlist (not extension)
- [ ] SHA-256 duplicate check returns 409 with `existing_document_id`
- [ ] MinIO webhook handler parses key path, verifies document in DB, publishes to `ingest_events`
- [ ] Webhook endpoint protected by HMAC secret comparison
- [ ] Redis event includes all fields from `docs/architecture.md §15` canonical schema
- [ ] Document status updated to `validating` after webhook receipt
- [ ] Audit log entry written for every initiated upload
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] All unit and security tests pass
- [ ] `ruff check . && mypy src/` exit zero
