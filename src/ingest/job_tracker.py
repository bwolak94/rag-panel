"""JobTracker — writes ingest stage progress to ingestion_jobs.steps JSONB array.

Every write is a SQL UPDATE (not ORM identity map) for safety in long-running processes.
Logs MUST NOT contain document text, prompt content, or PII.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.ingestion_job import IngestionJob


class JobTracker:
    def __init__(self, session: AsyncSession, job_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._job_id = job_id
        self._tenant_id = tenant_id

    async def begin_stage(self, stage: str) -> None:
        """Append a step record with status='running' and started_at."""
        now = datetime.now(UTC).isoformat()
        step_json = (
            f'[{{"stage": "{stage}", "status": "running", '
            f'"started_at": "{now}", "completed_at": null, '
            f'"error": null, "meta": {{}}}}]'
        )
        await self._session.execute(
            update(IngestionJob)
            .where(
                IngestionJob.id == self._job_id,
                IngestionJob.tenant_id == self._tenant_id,
            )
            .values(
                current_step=stage,
                steps=IngestionJob.steps.op("||")(step_json),
            )
        )
        await self._session.commit()

    async def complete_stage(self, stage: str, meta: dict[str, Any] | None = None) -> None:
        """Mark the matching step record as completed (fetch-mutate-update pattern)."""
        now = datetime.now(UTC).isoformat()
        result = await self._session.execute(
            select(IngestionJob.steps).where(IngestionJob.id == self._job_id)
        )
        steps: list[dict[str, Any]] = list(result.scalar_one() or [])
        for step in steps:
            if step["stage"] == stage:
                step["status"] = "completed"
                step["completed_at"] = now
                step["meta"] = meta or {}
                break
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == self._job_id)
            .values(steps=steps, updated_at=func.now())
        )
        await self._session.commit()

    async def fail_stage(self, stage: str, error: str) -> None:
        """Mark last step as failed and increment retry_count."""
        result = await self._session.execute(
            select(IngestionJob.steps, IngestionJob.retry_count).where(
                IngestionJob.id == self._job_id
            )
        )
        row = result.one()
        steps: list[dict[str, Any]] = list(row.steps or [])
        now = datetime.now(UTC).isoformat()
        if steps:
            steps[-1]["status"] = "failed"
            steps[-1]["completed_at"] = now
            steps[-1]["error"] = error[:2000]  # Truncate to avoid bloat
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == self._job_id)
            .values(
                steps=steps,
                retry_count=row.retry_count + 1,
                status="processing",  # Still processing, will retry
            )
        )
        await self._session.commit()

    async def set_job_status(self, status: str, *, completed: bool = False) -> None:
        values: dict[str, Any] = {"status": status}
        if completed:
            values["completed_at"] = datetime.now(UTC)
        await self._session.execute(
            update(IngestionJob).where(IngestionJob.id == self._job_id).values(**values)
        )
        await self._session.commit()
