"""Repository for Document and IngestionJob CRUD."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.document import Document
from src.db.models.ingestion_job import IngestionJob

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
        "text/html",
        "text/markdown",
        "text/plain",
    }
)

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

    async def list_by_tenant(
        self,
        tenant_id: uuid.UUID,
        *,
        collection_id: uuid.UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Document], int]:
        from sqlalchemy import func

        q = select(Document).where(
            Document.tenant_id == tenant_id,
            Document.status != "deleted",
        )
        count_q = select(func.count()).select_from(Document).where(
            Document.tenant_id == tenant_id,
            Document.status != "deleted",
        )

        if collection_id is not None:
            q = q.where(Document.collection_id == collection_id)
            count_q = count_q.where(Document.collection_id == collection_id)

        q = q.order_by(Document.created_at.desc()).offset(offset).limit(limit)

        rows = list((await self._session.execute(q)).scalars().all())
        total = (await self._session.execute(count_q)).scalar_one()
        return rows, total

    async def update_status(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID, status: str
    ) -> None:
        await self._session.execute(
            update(Document)
            .where(Document.id == document_id, Document.tenant_id == tenant_id)
            .values(status=status)
        )
        await self._session.flush()

    async def soft_delete(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self.update_status(document_id, tenant_id, "deleted")
