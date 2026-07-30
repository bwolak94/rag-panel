"""AdminReviewService — human-in-the-loop document review for needs_review documents.

Security rules:
- tenant_id ALWAYS from ctx.tenant_id — never from body or query.
- Every approve/reject writes audit_log BEFORE returning HTTP 200.
- Redis publish failure triggers Postgres rollback (no ghost approval).
- presigned URLs are not logged (they contain credentials).
- llm_raw_response is stripped before returning validation_result to caller.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.admin_review import (
    ApproveDocumentRequest,
    DocumentReviewDetail,
    IngestionJobDetail,
    IngestionStep,
    RejectDocumentRequest,
    ReviewDecisionResponse,
    ReviewQueueItem,
    ReviewQueueResponse,
    ValidationIssue,
    ValidationResultSchema,
)
from src.core.clients.minio_client import get_minio_client
from src.core.exceptions import InvalidDocumentStateError, NotFoundError
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.ingestion_job_repository import IngestionJobRepository
from src.domain.audit_service import AuditService
from src.domain.auth import UserContext

logger = structlog.get_logger(__name__)

_PREVIEW_TTL_SECONDS = 300  # 5 minutes — hard cap per security policy


def _build_validation_schema(raw: dict[str, Any] | None) -> ValidationResultSchema:
    """Map JSONB validation_result to schema, stripping llm_raw_response."""
    if not raw:
        return ValidationResultSchema()
    issues = [
        ValidationIssue(code=i["code"], message=i["message"])
        for i in raw.get("issues", [])
        if isinstance(i, dict) and "code" in i and "message" in i
    ]
    return ValidationResultSchema(
        detected_type=raw.get("detected_type"),
        category=raw.get("category"),
        quality_score=raw.get("quality_score"),
        confidence=raw.get("confidence"),
        pii_flags=raw.get("pii_flags", []),
        issues=issues,
    )


def _build_flag_summary(validation: ValidationResultSchema) -> str:
    parts: list[str] = []
    if validation.pii_flags:
        parts.append("PII: " + ", ".join(validation.pii_flags))
    if validation.quality_score is not None and validation.quality_score < 0.7:
        parts.append(f"Low quality ({validation.quality_score:.2f})")
    if not parts:
        parts.append("Flagged for review")
    return " · ".join(parts)


def _encode_cursor(created_at: datetime, doc_id: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}:{doc_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, id_str = raw.rsplit(":", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except Exception:
        return None


class AdminReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._doc_repo = DocumentRepository(session)
        self._job_repo = IngestionJobRepository(session)
        self._audit = AuditService(session)
        self._session = session

    async def get_review_queue(
        self,
        ctx: UserContext,
        *,
        collection_id: uuid.UUID | None = None,
        cursor: str | None = None,
        page_size: int = 20,
    ) -> ReviewQueueResponse:
        from sqlalchemy import func, select

        from src.db.models.collection import Collection
        from src.db.models.document import Document
        from src.db.models.user import User

        q = (
            select(
                Document,
                Collection.name.label("collection_name"),
                User.display_name.label("uploader_name"),
            )
            .join(Collection, Collection.id == Document.collection_id)
            .outerjoin(User, User.id == Document.uploaded_by)
            .where(
                Document.tenant_id == ctx.tenant_id,
                Document.status == "needs_review",
            )
        )
        count_q = (
            select(func.count())
            .select_from(Document)
            .where(
                Document.tenant_id == ctx.tenant_id,
                Document.status == "needs_review",
            )
        )

        if collection_id is not None:
            q = q.where(Document.collection_id == collection_id)
            count_q = count_q.where(Document.collection_id == collection_id)

        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded:
                cursor_ts, cursor_id = decoded
                q = q.where(
                    (Document.created_at < cursor_ts)
                    | ((Document.created_at == cursor_ts) & (Document.id < cursor_id))
                )

        q = q.order_by(Document.created_at.desc(), Document.id.desc()).limit(page_size + 1)

        rows = list((await self._session.execute(q)).all())
        total = (await self._session.execute(count_q)).scalar_one()
        has_next = len(rows) > page_size
        rows = rows[:page_size]

        items: list[ReviewQueueItem] = []
        next_cursor: str | None = None
        for row in rows:
            doc = row[0]
            col_name: str = row[1] or ""
            uploader_name: str = row[2] or "Unknown"
            vr = _build_validation_schema(doc.validation_result)
            items.append(
                ReviewQueueItem(
                    id=doc.id,
                    filename=doc.original_filename,
                    collection_id=doc.collection_id,
                    collection_name=col_name,
                    uploaded_by_id=doc.uploaded_by,
                    uploaded_by_name=uploader_name,
                    uploaded_at=doc.created_at,
                    validation_result=vr,
                    flag_summary=_build_flag_summary(vr),
                )
            )

        if has_next and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.uploaded_at, last.id)

        return ReviewQueueResponse(
            items=items,
            total_count=total,
            next_cursor=next_cursor,
            has_next_page=has_next,
        )

    async def get_document_review_detail(
        self, ctx: UserContext, document_id: uuid.UUID
    ) -> DocumentReviewDetail:
        from sqlalchemy import select

        from src.db.models.collection import Collection
        from src.db.models.document import Document
        from src.db.models.user import User

        q = (
            select(
                Document,
                Collection.name.label("collection_name"),
                User.display_name.label("uploader_name"),
            )
            .join(Collection, Collection.id == Document.collection_id)
            .outerjoin(User, User.id == Document.uploaded_by)
            .where(Document.id == document_id, Document.tenant_id == ctx.tenant_id)
        )
        row = (await self._session.execute(q)).first()
        if row is None:
            raise NotFoundError(f"Document {document_id} not found")
        doc, col_name, uploader_name = row[0], row[1] or "", row[2] or "Unknown"

        if doc.status != "needs_review":
            raise InvalidDocumentStateError(
                "Document is not awaiting review", current_status=doc.status
            )

        job = await self._doc_repo.get_latest_job(document_id, ctx.tenant_id)
        if job is None:
            raise NotFoundError(f"No ingestion job found for document {document_id}")

        # Generate presigned GET URL — NOT logged (contains credentials)
        bucket = f"tenant-{ctx.tenant_id}"
        preview_url: str = await asyncio.to_thread(
            lambda: get_minio_client().presigned_get_object(
                bucket, doc.minio_key, expires=timedelta(seconds=_PREVIEW_TTL_SECONDS)
            )
        )
        preview_expires_at = datetime.now(UTC) + timedelta(seconds=_PREVIEW_TTL_SECONDS)

        steps = [
            IngestionStep(
                stage=s.get("stage", ""),
                status=s.get("status", ""),
                started_at=s.get("started_at"),
                completed_at=s.get("completed_at"),
                error=s.get("error"),
                meta=s.get("meta", {}),
            )
            for s in (job.steps or [])
        ]

        return DocumentReviewDetail(
            id=doc.id,
            filename=doc.original_filename,
            collection_id=doc.collection_id,
            collection_name=col_name,
            uploaded_by_id=doc.uploaded_by,
            uploaded_by_name=uploader_name,
            uploaded_at=doc.created_at,
            size_bytes=doc.size_bytes,
            mime_type=doc.mime_type,
            validation_result=_build_validation_schema(doc.validation_result),
            ingestion_steps=steps,
            ingestion_job_id=job.id,
            preview_url=preview_url,
            preview_url_expires_at=preview_expires_at,
        )

    async def approve_document(
        self,
        ctx: UserContext,
        document_id: uuid.UUID,
        body: ApproveDocumentRequest,
    ) -> ReviewDecisionResponse:
        from src.core.clients.redis_client import get_redis_client

        doc = await self._doc_repo.get_by_id(document_id, ctx.tenant_id)
        if doc is None:
            raise NotFoundError(f"Document {document_id} not found")
        if doc.status != "needs_review":
            raise InvalidDocumentStateError(
                "Document is not awaiting review", current_status=doc.status
            )

        decided_at = datetime.now(UTC)

        # Audit MUST be written atomically with the status change
        await self._doc_repo.set_reviewed(
            document_id,
            ctx.tenant_id,
            status="queued",
            reviewed_by=ctx.user_id,
            reviewed_at=decided_at,
        )
        await self._audit.log(
            ctx=ctx,
            action="document.approved",
            resource_type="document",
            resource_id=document_id,
            details={
                "previous_status": "needs_review",
                "reviewer_role": sorted(ctx.roles)[0] if ctx.roles else "unknown",
                # note is metadata only — not document content
                "note_provided": body.note is not None,
            },
        )

        # Publish resume event — failure must roll back Postgres transaction
        try:
            redis = get_redis_client()
            await redis.xadd(
                "ingest_events",
                {
                    "document_id": str(document_id),
                    "tenant_id": str(ctx.tenant_id),
                    "action": "resume",
                    "schema_version": "1",
                },
            )
        except Exception as exc:
            logger.error(
                "admin_review_redis_publish_failed",
                document_id=str(document_id),
                error_type=type(exc).__name__,
            )
            # Raise to trigger router-level rollback
            raise

        logger.info(
            "document_approved",
            document_id=str(document_id),
            reviewer_id=str(ctx.user_id),
        )
        return ReviewDecisionResponse(
            document_id=document_id,
            new_status="queued",
            decided_at=decided_at,
            decided_by_id=ctx.user_id,
            message="Document approved and returned to processing queue.",
        )

    async def reject_document(
        self,
        ctx: UserContext,
        document_id: uuid.UUID,
        body: RejectDocumentRequest,
    ) -> ReviewDecisionResponse:
        doc = await self._doc_repo.get_by_id(document_id, ctx.tenant_id)
        if doc is None:
            raise NotFoundError(f"Document {document_id} not found")
        if doc.status != "needs_review":
            raise InvalidDocumentStateError(
                "Document is not awaiting review", current_status=doc.status
            )

        decided_at = datetime.now(UTC)

        await self._doc_repo.set_reviewed(
            document_id,
            ctx.tenant_id,
            status="rejected",
            reviewed_by=ctx.user_id,
            reviewed_at=decided_at,
        )
        await self._audit.log(
            ctx=ctx,
            action="document.rejected",
            resource_type="document",
            resource_id=document_id,
            details={
                "previous_status": "needs_review",
                "reviewer_role": sorted(ctx.roles)[0] if ctx.roles else "unknown",
                # reason is NOT stored in logs — only in audit_log details
            },
        )

        logger.info(
            "document_rejected",
            document_id=str(document_id),
            reviewer_id=str(ctx.user_id),
        )
        return ReviewDecisionResponse(
            document_id=document_id,
            new_status="rejected",
            decided_at=decided_at,
            decided_by_id=ctx.user_id,
            message="Document rejected. The uploader can be notified via audit log.",
        )

    async def get_ingestion_job(self, ctx: UserContext, job_id: uuid.UUID) -> IngestionJobDetail:
        from sqlalchemy import select

        from src.db.models.ingestion_job import IngestionJob

        q = select(IngestionJob).where(
            IngestionJob.id == job_id,
            IngestionJob.tenant_id == ctx.tenant_id,
        )
        job = (await self._session.execute(q)).scalar_one_or_none()
        if job is None:
            raise NotFoundError(f"Ingestion job {job_id} not found")

        steps = job.steps or []
        current_step: str | None = None
        for s in reversed(steps):
            if isinstance(s, dict) and s.get("status") in ("running", "awaiting_review"):
                current_step = s.get("stage")
                break

        return IngestionJobDetail(
            id=job.id,
            document_id=job.document_id,
            status=job.status,
            current_step=current_step,
            retry_count=job.retry_count,
            steps=[
                IngestionStep(
                    stage=s.get("stage", ""),
                    status=s.get("status", ""),
                    started_at=s.get("started_at"),
                    completed_at=s.get("completed_at"),
                    error=s.get("error"),
                    meta=s.get("meta", {}),
                )
                for s in steps
            ],
            started_at=job.started_at,
            completed_at=job.completed_at,
            created_at=job.created_at,
        )
