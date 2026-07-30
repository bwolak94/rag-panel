"""EventProcessor — validates, deduplicates, and dispatches ingest events.

Implements retry logic with exponential backoff + jitter per architecture §16.
Raises MaxRetriesExceededError when retry_count >= MAX_RETRIES so the worker
can move the event to the dead-letter stream.
"""

from __future__ import annotations

import asyncio
import random

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.repositories.document_repository import DocumentRepository
from src.db.repositories.ingestion_job_repository import IngestionJobRepository
from src.ingest.schemas import IngestEvent

logger = structlog.get_logger(__name__)

MAX_RETRIES: int = settings.INGEST_NODE_MAX_RETRIES
BASE_DELAY_S: float = 2.0
MAX_DELAY_S: float = 30.0
JITTER_PCT: float = 0.2


def compute_backoff(attempt: int) -> float:
    """
    Exponential backoff with additive jitter.
    Formula per docs/architecture.md §16:
        min(base * 2^attempt * (1 + jitter), max_delay)
    Jitter is always additive (≥ 0) so delay never decreases below the base value.
    """
    base: float = BASE_DELAY_S * (2**attempt)
    jitter: float = base * random.uniform(0, JITTER_PCT)
    result: float = min(base + jitter, MAX_DELAY_S)
    return result


class MaxRetriesExceededError(Exception):
    def __init__(self, reason: str, failure_count: int) -> None:
        super().__init__(reason)
        self.failure_count = failure_count


class EventProcessor:
    async def process(self, session: AsyncSession, fields: dict[str, str]) -> bool:
        """
        Returns True on success (caller should XACK).
        Returns False on temporary failure (caller should not XACK — retry later).
        Raises MaxRetriesExceededError after MAX_RETRIES failures or on unrecoverable error.
        """
        # 1. Validate schema version — unknown versions go to DLQ immediately (no retries)
        schema_version = fields.get("schema_version", "")
        if schema_version != "1":
            raise MaxRetriesExceededError(
                f"schema_mismatch: received version '{schema_version}'",
                failure_count=1,
            )

        # 2. Parse and validate event
        try:
            event = IngestEvent(**{k: v for k, v in fields.items()})
        except Exception as exc:
            raise MaxRetriesExceededError(f"event_parse_error: {exc}", failure_count=1) from exc

        # 3. Idempotency check: is this document already fully processed?
        doc = await DocumentRepository(session).get_by_id(event.document_id, event.tenant_id)
        if doc is None:
            logger.error(
                "document_not_found_in_event",
                document_id=str(event.document_id),
                tenant_id=str(event.tenant_id),
            )
            raise MaxRetriesExceededError("document_not_found", failure_count=1)

        if doc.status == "ready":
            logger.info(
                "document_already_processed_idempotent_skip",
                document_id=str(event.document_id),
            )
            return True  # Idempotent: already done, caller should XACK

        # 4. Load or create ingestion job
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_active_job(event.document_id, event.tenant_id)
        if job is None:
            # Should not happen — job was created in TASK-006; defensive fallback
            job = await job_repo.create(
                tenant_id=event.tenant_id,
                document_id=event.document_id,
            )

        # 5. Check retry count before attempting
        if job.retry_count >= MAX_RETRIES:
            raise MaxRetriesExceededError(
                f"max_retries_exceeded after {job.retry_count} attempts",
                failure_count=job.retry_count,
            )

        # 6. Dispatch to ingest graph
        try:
            from src.graphs.ingest_graph.graph import run_ingest_graph

            await run_ingest_graph(
                event=event,
                job_id=job.id,
                session=session,
            )
            return True

        except Exception as exc:
            from src.ingest.job_tracker import JobTracker

            tracker = JobTracker(session, job.id, event.tenant_id)
            await tracker.fail_stage(
                stage=doc.status or "unknown",
                error=str(exc),
            )
            delay = compute_backoff(job.retry_count)
            logger.warning(
                "ingest_attempt_failed",
                document_id=str(event.document_id),
                attempt=job.retry_count,
                backoff_s=delay,
            )
            await asyncio.sleep(delay)
            return False
