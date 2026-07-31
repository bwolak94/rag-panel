"""DocumentService — document upload initiation and management."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.minio_client import get_minio_client
from src.core.config import settings
from src.core.exceptions import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    PermissionDeniedError,
)
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.tenant_repository import TenantRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext
from src.domain.schemas.document import (
    DocumentUploadRequest,
    DocumentUploadResponse,
)

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
            raise ConflictError(f"Duplicate document. existing_document_id={existing.id}")

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

        # 6. Generate presigned URL (sync minio-py → asyncio.to_thread)
        upload_url: str = await asyncio.to_thread(
            lambda: get_minio_client().presigned_put_object(
                bucket, minio_key, expires=timedelta(seconds=ttl_seconds)
            )
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

    async def list_review_queue(
        self,
        ctx: UserContext,
        *,
        offset: int,
        limit: int,
    ) -> tuple[list[Any], int]:
        """Return paginated documents awaiting admin review for this tenant.

        Requires documents:approve permission (enforced at router level).
        All documents returned include validation_result (admins only).

        Args:
            ctx: Authenticated user context from JWT.
            offset: Number of records to skip.
            limit: Maximum records per page.

        Returns:
            Tuple of (raw Document ORM objects, total count).
        """
        docs, total = await self._repo.list_needs_review(
            ctx.tenant_id,
            offset=offset,
            limit=limit,
        )

        logger.info(
            "review_queue_listed",
            tenant_id=str(ctx.tenant_id),
            count=len(docs),
            total=total,
        )

        return docs, total

    async def review_document(
        self,
        *,
        document_id: uuid.UUID,
        decision: Literal["approve", "reject"],
        note: str | None,
        ctx: UserContext,
        ip: str | None,
    ) -> uuid.UUID | None:
        """Process admin review decision for a needs_review document.

        Args:
            document_id: Document to review.
            decision: "approve" or "reject".
            note: Optional admin note (not logged for privacy).
            ctx: Authenticated user context from JWT.
            ip: Client IP for audit log.

        Returns:
            job.id when decision is "approve" (caller dispatches background ingest),
            None when decision is "reject".

        Raises:
            NotFoundError: If document not found or wrong tenant.
            DomainValidationError: If document is not in needs_review status or
                decision is invalid.
        """
        from src.db.models.ingestion_job import IngestionJob

        doc = await self._repo.get_by_id(document_id, ctx.tenant_id)
        if doc is None:
            raise NotFoundError(f"Document {document_id} not found")

        if doc.status != "needs_review":
            raise DomainValidationError(
                f"Document {document_id} is in status '{doc.status}', expected 'needs_review'"
            )

        reviewed_at = datetime.now(UTC)

        if decision == "approve":
            await self._repo.set_reviewed(
                document_id,
                ctx.tenant_id,
                status="indexing",
                reviewed_by=ctx.user_id,
                reviewed_at=reviewed_at,
            )

            # Retrieve the latest job for this document to resume
            job = await self._repo.get_latest_job(document_id, ctx.tenant_id)
            if job is None:
                raise NotFoundError(f"No ingestion job found for document {document_id}")

            await self._audit.log(
                ctx=ctx,
                action="document.review_approved",
                resource_type="document",
                resource_id=document_id,
                details={"decision": "approve"},
                ip=ip,
            )

            logger.info(
                "document_review_approved",
                document_id=str(document_id),
                tenant_id=str(ctx.tenant_id),
            )

            return job.id

        elif decision == "reject":
            await self._repo.set_reviewed(
                document_id,
                ctx.tenant_id,
                status="rejected",
                reviewed_by=ctx.user_id,
                reviewed_at=reviewed_at,
            )

            # Mark the associated job as completed
            job = await self._repo.get_latest_job(document_id, ctx.tenant_id)
            if job is not None:
                await self._session.execute(
                    sa_update(IngestionJob)
                    .where(IngestionJob.id == job.id)
                    .values(status="completed")
                )
                await self._session.flush()

            await self._audit.log(
                ctx=ctx,
                action="document.review_rejected",
                resource_type="document",
                resource_id=document_id,
                details={"decision": "reject"},
                ip=ip,
            )

            logger.info(
                "document_review_rejected",
                document_id=str(document_id),
                tenant_id=str(ctx.tenant_id),
            )

            return None

        else:
            raise DomainValidationError(f"Invalid review decision: {decision!r}")
