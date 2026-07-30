"""Shared helpers for ingest graph nodes.

update_step() is the single audit trail writer for the pipeline.
get_collection() and get_model() cache DB reads from state when possible.
Never log document content, PII, raw bytes, or prompt text.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.collection import Collection
from src.db.models.ingestion_job import IngestionJob
from src.db.models.models_registry import ModelsRegistry


def utcnow() -> datetime:
    return datetime.now(UTC)


async def update_step(
    session: AsyncSession,
    job_id: uuid.UUID,
    stage: str,
    status: str,
    started_at: datetime,
    meta: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Append a step entry to ingestion_jobs.steps and commit.

    Records completed_at = now(), truncates error to 2000 chars.
    Never include document content or PII in meta.
    """
    step: dict[str, Any] = {
        "stage": stage,
        "status": status,
        "started_at": started_at.isoformat(),
        "completed_at": utcnow().isoformat(),
        "error": (error or "")[:2000] or None,
        "meta": meta or {},
    }
    step_json = f"[{json.dumps(step)}]"
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            current_step=stage,
            steps=IngestionJob.steps.op("||")(step_json),
        )
    )
    await session.commit()


async def get_collection(session: AsyncSession, collection_id: uuid.UUID) -> Collection:
    """Fetch a collection by ID. Raises ValueError if not found."""
    from sqlalchemy import select

    result = await session.execute(select(Collection).where(Collection.id == collection_id))
    collection = result.scalar_one_or_none()
    if collection is None:
        raise ValueError(f"Collection {collection_id} not found")
    return collection


async def get_model(session: AsyncSession, model_id: uuid.UUID) -> ModelsRegistry:
    """Fetch a model record by ID. Raises ValueError if not found."""
    from sqlalchemy import select

    result = await session.execute(select(ModelsRegistry).where(ModelsRegistry.id == model_id))
    model = result.scalar_one_or_none()
    if model is None:
        raise ValueError(f"Model {model_id} not found")
    return model
