"""Repository for IngestionJob lifecycle management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.ingestion_job import IngestionJob


class IngestionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active_job(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> IngestionJob | None:
        """Returns the most recent non-completed job for a document."""
        q = (
            select(IngestionJob)
            .where(
                IngestionJob.document_id == document_id,
                IngestionJob.tenant_id == tenant_id,
                IngestionJob.status.in_(["pending", "processing"]),
            )
            .order_by(IngestionJob.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def create(
        self, tenant_id: uuid.UUID, document_id: uuid.UUID
    ) -> IngestionJob:
        job = IngestionJob(
            tenant_id=tenant_id,
            document_id=document_id,
            status="pending",
            steps=[],
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def mark_processing(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .values(status="processing", started_at=datetime.now(UTC))
        )
        await self._session.commit()

    async def mark_completed(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .values(status="completed", completed_at=datetime.now(UTC))
        )
        await self._session.commit()

    async def mark_failed(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .values(status="failed", completed_at=datetime.now(UTC))
        )
        await self._session.commit()
