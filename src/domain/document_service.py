"""DocumentService — document upload initiation and management."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.document import DocumentUploadRequest, DocumentUploadResponse
from src.core.clients.minio_client import get_minio_client
from src.core.config import settings
from src.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

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

        # 2. SHA-256 dedup within tenant
        existing = await self._repo.check_duplicate(ctx.tenant_id, body.sha256)
        if existing is not None:
            raise ConflictError(
                f"Duplicate document. existing_document_id={existing.id}"
            )

        # 3. Compute MinIO key (filename already sanitized by Pydantic validator)
        doc_id = uuid.uuid4()
        minio_key = f"raw/{body.collection_id}/{doc_id}/{body.filename}"

        # 4. Insert DB records (flush only — router commits after this returns)
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

        # 5. Resolve tenant slug for bucket name
        tenant = await self._tenant_repo.get_by_id(ctx.tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found")

        bucket = f"tenant-{tenant.slug}"
        ttl_seconds = settings.INGEST_PRESIGNED_URL_TTL_SECONDS
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

        # 6. Generate presigned URL (sync minio-py → run_in_executor)
        loop = asyncio.get_event_loop()
        upload_url: str = await loop.run_in_executor(
            None,
            lambda: get_minio_client().presigned_put_object(
                bucket, minio_key, expires=timedelta(seconds=ttl_seconds)
            ),
        )

        # 7. Audit — details contain only IDs and metadata, never file content
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

        # 8. Commit — router owns transaction; service commits here per TASK-006 spec
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
