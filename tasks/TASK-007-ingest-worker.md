# TASK-007: Ingest Worker

**Status:** TODO
**Priority:** P1 — required after TASK-006
**Owner:** backend-dev
**Reviewer:** python-reviewer, rag-engineer
**Related docs:** `docs/architecture.md` §5, §8, §15, §16, §17 | `docs/data-model.md` §2.2
**Estimated effort:** 5–7 days

---

## Overview

Implement the Redis Streams consumer worker that processes document ingest events published by the MinIO webhook handler (TASK-006). The worker runs in a separate process from the API, consuming from the `ingest_events` stream using consumer groups (`XREADGROUP`), dispatching events to the LangGraph ingest graph, and acknowledging processed events with `XACK`.

Failure handling: after 3 consecutive processing failures (tracked in `ingestion_jobs.retry_count`), the event is moved to the dead-letter stream `ingest_events_dlq`, the document status is set to `failed`, and a Prometheus alert is triggered. Each stage of the ingest graph reports its status to `ingestion_jobs.steps` (the JSONB array of step records). Processing is idempotent: re-processing an event that was already successfully completed is a no-op.

## Usage

The worker process is started separately from the API:
```shell
# Standalone worker process
uv run python -m src.ingest.worker
```

In Docker Compose, it runs as the `ingest-worker` service with its own container:
```yaml
ingest-worker:
  command: python -m src.ingest.worker
  depends_on:
    postgres: {condition: service_healthy}
    redis:    {condition: service_healthy}
    minio:    {condition: service_healthy}
    qdrant:   {condition: service_healthy}
```

The worker exposes a `/health` endpoint on port 9090 (FastAPI app, separate from main API) and publishes Prometheus metrics at `/metrics` (per `docs/architecture.md §17`).

**Scaling:** Multiple worker instances run in the same consumer group `ingest_workers`. Each instance has a unique consumer name: `worker-{hostname}-{pid}`. Redis distributes events across consumers; no two workers process the same event simultaneously.

## Tech Stack

- **redis-py 5.2+** — async `XREADGROUP`, `XACK`, `XADD` (for DLQ), `XCLAIM` (for abandoned message recovery)
- **asyncpg / SQLAlchemy 2.x** — `AsyncSession` for DB updates in each processing stage
- **langgraph-checkpoint-postgres** — Postgres checkpointer for ingest graph state; checkpoint thread_id stored in `ingestion_jobs.langgraph_thread_id`
- **prometheus-client** — metrics: `ingest_jobs_total`, `ingest_job_duration_seconds`, `ingest_dlq_length`
- **structlog** — structured logging; worker logs include `worker_id`, `document_id`, `tenant_id` for log correlation
- **FastAPI** — minimal health/metrics server on port 9090

## Database Patterns

### Tables Written by Worker

- `ingestion_jobs` — status updates at each stage; `steps` JSONB array appended per stage; `retry_count` incremented on failure
- `documents` — status updates: `validating → indexing → ready` (success path), `→ failed` (failure path), `→ needs_review` (PII/low-confidence path)
- `chunks_registry` — INSERT rows in `persist_status` stage
- `audit_log` — `document.indexed` on success; `document.ingest_failed` on permanent failure

### IngestionJob Status Update Pattern

```python
# src/ingest/job_tracker.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import IngestionJob, Document


class JobTracker:
    """
    Writes stage progress to ingestion_jobs.steps JSONB array.
    Every write is a SQL UPDATE (not ORM identity map) for safety in long-running processes.
    """
    def __init__(self, session: AsyncSession, job_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._job_id = job_id
        self._tenant_id = tenant_id

    async def begin_stage(self, stage: str) -> None:
        """Append a step record with status='running' and started_at."""
        now = datetime.now(timezone.utc).isoformat()
        await self._session.execute(
            update(IngestionJob)
            .where(
                IngestionJob.id == self._job_id,
                IngestionJob.tenant_id == self._tenant_id,
            )
            .values(
                current_step=stage,
                steps=IngestionJob.steps.op("||")(
                    f'[{{"stage": "{stage}", "status": "running", '
                    f'"started_at": "{now}", "completed_at": null, '
                    f'"error": null, "meta": {{}}}}]'
                ),
            )
        )
        await self._session.commit()

    async def complete_stage(self, stage: str, meta: dict | None = None) -> None:
        """Mark the matching step record as completed (fetch-mutate-update pattern)."""
        now = datetime.now(timezone.utc).isoformat()
        from sqlalchemy import select
        result = await self._session.execute(
            select(IngestionJob.steps).where(IngestionJob.id == self._job_id)
        )
        steps: list = list(result.scalar_one() or [])
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
        from sqlalchemy import select
        result = await self._session.execute(
            select(IngestionJob.steps, IngestionJob.retry_count)
            .where(IngestionJob.id == self._job_id)
        )
        row = result.one()
        steps: list = list(row.steps or [])
        now = datetime.now(timezone.utc).isoformat()
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

    async def set_job_status(
        self, status: str, *, completed: bool = False
    ) -> None:
        values: dict = {"status": status}
        if completed:
            values["completed_at"] = datetime.now(timezone.utc)
        await self._session.execute(
            update(IngestionJob).where(IngestionJob.id == self._job_id).values(**values)
        )
        await self._session.commit()
```

## API Contracts (Internal Events)

### Consumed Event Schema (from `ingest_events`)

Per `docs/architecture.md §15` (canonical definition):

```python
# src/ingest/schemas.py
from pydantic import BaseModel, field_validator
import uuid
from datetime import datetime


class IngestEvent(BaseModel):
    """
    Canonical event from Redis Streams ingest_events.
    schema_version must be "1" — unknown versions route to DLQ.
    """
    schema_version: str
    event_type: str
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    collection_id: uuid.UUID
    minio_bucket: str
    minio_key: str
    size_bytes: int
    content_type: str
    published_at: datetime

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if v != "1":
            raise ValueError(f"Unknown schema_version: {v}. Expected '1'.")
        return v


class DeadLetterEvent(BaseModel):
    """Event written to ingest_events_dlq after max retries."""
    original_event: dict
    failure_reason: str
    failure_count: int
    last_failed_at: datetime
    document_id: uuid.UUID
    tenant_id: uuid.UUID
```

## Architecture — SOLID & DRY

### Worker Main Loop

```python
# src/ingest/worker.py
"""
Ingest Worker — Redis Streams consumer.

Run with: python -m src.ingest.worker
"""
import asyncio
import os
import signal
import socket
import uuid
from datetime import datetime, timezone

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from src.core.clients.redis_client import get_redis_client
from src.core.config import settings
from src.core.database import AsyncSessionLocal
from src.core.logging import configure_logging
from src.ingest.event_processor import EventProcessor

logger = structlog.get_logger(__name__)

# Prometheus metrics
JOBS_TOTAL = Counter("ingest_jobs_total", "Total ingest jobs", ["status"])
JOB_DURATION = Histogram(
    "ingest_job_duration_seconds", "Ingest job duration", ["status"]
)
DLQ_LENGTH = Gauge("ingest_dlq_length", "Dead letter queue length")
WORKER_HEARTBEAT = Gauge(
    "ingest_worker_last_heartbeat_timestamp",
    "Unix timestamp of last worker heartbeat",
    ["worker_id"],
)

STREAM_NAME = "ingest_events"
CONSUMER_GROUP = "ingest_workers"
DLQ_STREAM = "ingest_events_dlq"
MAX_RETRIES = 3
BLOCK_MS = 5000


class IngestWorker:
    def __init__(self) -> None:
        self._worker_id = f"worker-{socket.gethostname()}-{os.getpid()}"
        self._running = False
        self._processor = EventProcessor()

    async def start(self) -> None:
        configure_logging()
        redis = await get_redis_client()

        # Ensure consumer group exists
        try:
            await redis.xgroup_create(
                STREAM_NAME, CONSUMER_GROUP, id="$", mkstream=True
            )
        except Exception:
            pass  # Group already exists

        logger.info("worker_started", worker_id=self._worker_id)
        self._running = True
        await asyncio.gather(
            self._consume_loop(redis),
            self._heartbeat_loop(redis),
            self._recover_abandoned_messages(redis),
        )

    async def _consume_loop(self, redis) -> None:  # type: ignore[no-untyped-def]
        while self._running:
            try:
                messages = await redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=self._worker_id,
                    streams={STREAM_NAME: ">"},
                    count=1,
                    block=BLOCK_MS,
                )
                for _stream, entries in (messages or []):
                    for message_id, fields in entries:
                        await self._process_message(redis, message_id, fields)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("consume_loop_error", error=str(exc))
                await asyncio.sleep(1)

    async def _process_message(
        self,
        redis,  # type: ignore[no-untyped-def]
        message_id: str,
        fields: dict,
    ) -> None:
        start = datetime.now(timezone.utc)
        document_id = fields.get("document_id", "unknown")

        try:
            async with AsyncSessionLocal() as session:
                success = await self._processor.process(session, fields)

            if success:
                await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
                JOBS_TOTAL.labels(status="success").inc()
                duration = (datetime.now(timezone.utc) - start).total_seconds()
                JOB_DURATION.labels(status="success").observe(duration)
                logger.info("ingest_event_acked", document_id=document_id)
            else:
                # processor returned False — temporary failure, don't ack
                # Message stays in PEL; will be retried or recovered
                JOBS_TOTAL.labels(status="retry").inc()

        except MaxRetriesExceededError as exc:
            await self._move_to_dlq(redis, message_id, fields, str(exc), exc.failure_count)
            await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)
            JOBS_TOTAL.labels(status="dlq").inc()
            logger.error(
                "event_moved_to_dlq",
                document_id=document_id,
                reason=str(exc),
            )

    async def _move_to_dlq(
        self,
        redis,  # type: ignore[no-untyped-def]
        message_id: str,
        original_fields: dict,
        reason: str,
        failure_count: int,
    ) -> None:
        dlq_entry = {
            "original_event": str(original_fields),
            "failure_reason": reason[:500],
            "failure_count": str(failure_count),
            "last_failed_at": datetime.now(timezone.utc).isoformat(),
            "document_id": original_fields.get("document_id", ""),
            "tenant_id": original_fields.get("tenant_id", ""),
        }
        await redis.xadd(DLQ_STREAM, dlq_entry)

        # Update document status to failed
        doc_id_str = original_fields.get("document_id", "")
        tenant_id_str = original_fields.get("tenant_id", "")
        if doc_id_str and tenant_id_str:
            try:
                async with AsyncSessionLocal() as session:
                    from src.db.repositories.document_repository import DocumentRepository
                    await DocumentRepository(session).update_status(
                        uuid.UUID(doc_id_str), uuid.UUID(tenant_id_str), "failed"
                    )
                    await session.commit()
            except Exception as db_exc:
                logger.error("dlq_status_update_failed", error=str(db_exc))

        # Update Prometheus gauge
        dlq_len = await redis.xlen(DLQ_STREAM)
        DLQ_LENGTH.set(dlq_len)

    async def _heartbeat_loop(self, redis) -> None:  # type: ignore[no-untyped-def]
        """Publishes heartbeat every 30 seconds. See docs/architecture.md §17."""
        self._heartbeat_failures = 0
        self._parent_task: asyncio.Task = asyncio.current_task()  # type: ignore[assignment]
        while self._running:
            try:
                ts = datetime.now(timezone.utc).timestamp()
                await redis.set(
                    f"worker:heartbeat:{self._worker_id}",
                    str(ts),
                    ex=120,
                )
                WORKER_HEARTBEAT.labels(worker_id=self._worker_id).set(ts)
                self._heartbeat_failures = 0
            except Exception as exc:
                logger.error("heartbeat_error", error=str(exc), job_id=str(self._worker_id))
                self._heartbeat_failures += 1
                if self._heartbeat_failures >= 3:
                    self._parent_task.cancel()
            await asyncio.sleep(30)

    async def _recover_abandoned_messages(self, redis) -> None:  # type: ignore[no-untyped-def]
        """
        Periodically claim messages idle > 60s from other consumers (crash recovery).
        Per architecture §16: dead consumers' pending messages are reclaimed.
        """
        while self._running:
            await asyncio.sleep(60)
            try:
                # Claim messages idle > 60 seconds
                claimed = await redis.xautoclaim(
                    STREAM_NAME,
                    CONSUMER_GROUP,
                    self._worker_id,
                    min_idle_time=60_000,  # ms
                    start_id="0-0",
                    count=10,
                )
                if claimed and claimed[1]:
                    logger.info(
                        "claimed_abandoned_messages",
                        count=len(claimed[1]),
                        worker_id=self._worker_id,
                    )
            except Exception as exc:
                logger.warning("recover_loop_error", error=str(exc))

    def stop(self) -> None:
        self._running = False


class MaxRetriesExceededError(Exception):
    def __init__(self, reason: str, failure_count: int) -> None:
        super().__init__(reason)
        self.failure_count = failure_count


async def main() -> None:
    worker = IngestWorker()
    loop = asyncio.get_event_loop()

    def _shutdown(sig):  # type: ignore[no-untyped-def]
        logger.info("shutdown_signal_received", signal=sig)
        worker.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    # Metrics server on port 9090 (not the main API)
    start_http_server(9090)
    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
```

### Event Processor with Retry Logic

```python
# src/ingest/event_processor.py
import asyncio
import random
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.ingest.schemas import IngestEvent
from src.db.repositories.document_repository import DocumentRepository

logger = structlog.get_logger(__name__)

MAX_RETRIES = settings.INGEST_NODE_MAX_RETRIES
BASE_DELAY_S = 2.0
MAX_DELAY_S = 30.0
JITTER_PCT = 0.2


def compute_backoff(attempt: int) -> float:
    """
    Exponential backoff with jitter.
    Formula from docs/architecture.md §16:
    min(base * 2^attempt * (1 + jitter), max_delay)
    Jitter is always additive (≥ 0) so delay never decreases below the base value.
    """
    base = BASE_DELAY_S * (2 ** attempt)
    jitter = base * random.uniform(0, JITTER_PCT)
    return min(base + jitter, MAX_DELAY_S)


class EventProcessor:
    async def process(self, session: AsyncSession, fields: dict) -> bool:
        """
        Returns True on success (caller should XACK).
        Returns False on temporary failure (caller should not XACK — retry later).
        Raises MaxRetriesExceededError after MAX_RETRIES failures.
        """
        # 1. Validate schema version
        schema_version = fields.get("schema_version", "")
        if schema_version != "1":
            # Unknown schema version → route to DLQ immediately (no retries)
            raise MaxRetriesExceededError(  # type: ignore[misc]
                f"schema_mismatch: received version '{schema_version}'",
                failure_count=1,
            )

        # 2. Parse and validate event
        try:
            event = IngestEvent(**{k: v for k, v in fields.items()})
        except Exception as exc:
            raise MaxRetriesExceededError(f"event_parse_error: {exc}", failure_count=1)  # type: ignore[misc]

        # 3. Idempotency check: is this document already processed?
        doc = await DocumentRepository(session).get_by_id(
            event.document_id, event.tenant_id
        )
        if doc is None:
            logger.error(
                "document_not_found_in_event",
                document_id=str(event.document_id),
                tenant_id=str(event.tenant_id),
            )
            raise MaxRetriesExceededError("document_not_found", failure_count=1)  # type: ignore[misc]

        if doc.status == "ready":
            logger.info(
                "document_already_processed_idempotent_skip",
                document_id=str(event.document_id),
            )
            return True  # Idempotent: already done, XACK

        # 4. Load or create ingestion job
        from src.db.repositories.ingestion_job_repository import IngestionJobRepository
        job_repo = IngestionJobRepository(session)
        job = await job_repo.get_active_job(event.document_id, event.tenant_id)
        if job is None:
            # Should not happen — job was created in TASK-006; defensive fallback
            job = await job_repo.create(
                tenant_id=event.tenant_id,
                document_id=event.document_id,
            )

        # 5. Check retry count
        if job.retry_count >= MAX_RETRIES:
            raise MaxRetriesExceededError(  # type: ignore[misc]
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
            # Exponential backoff before returning False
            delay = compute_backoff(job.retry_count)
            logger.warning(
                "ingest_attempt_failed",
                document_id=str(event.document_id),
                attempt=job.retry_count,
                backoff_s=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)
            return False
```

### Ingest Graph Stub

```python
# src/graphs/ingest_graph/graph.py
"""
LangGraph ingest graph.
Full implementation is the responsibility of rag-engineer.
This module provides the entry point that the worker calls.
"""
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from src.ingest.schemas import IngestEvent


async def run_ingest_graph(
    event: IngestEvent,
    job_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """
    Executes the ingest LangGraph.
    Nodes: fetch_from_minio → extract_text → dedupe_check → llm_validate
           → pii_scan → chunk → embed → upsert_qdrant → persist_status

    Each node updates ingestion_jobs.steps via JobTracker.
    Graph state is checkpointed to Postgres via LangGraph checkpointer.
    thread_id is stored in ingestion_jobs.langgraph_thread_id.

    Raises exception on unrecoverable failure.
    On needs_review: sets document.status='needs_review', job.status='awaiting_review',
    saves LangGraph checkpoint, and returns without exception.
    """
    # Implementation by rag-engineer (TASK-008 and beyond)
    raise NotImplementedError("Ingest graph not yet implemented")
```

### Ingestion Job Repository

```python
# src/db/repositories/ingestion_job_repository.py
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.db.models import IngestionJob


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
            .values(status="processing", started_at=datetime.now(timezone.utc))
        )
        await self._session.commit()

    async def mark_completed(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .values(status="completed", completed_at=datetime.now(timezone.utc))
        )
        await self._session.commit()

    async def mark_failed(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == job_id, IngestionJob.tenant_id == tenant_id)
            .values(status="failed", completed_at=datetime.now(timezone.utc))
        )
        await self._session.commit()
```

## Implementation Steps

1. **Create `src/ingest/` package structure:**
   ```
   src/ingest/
     __init__.py
     worker.py         # Main consumer loop, heartbeat, recovery
     event_processor.py # Retry logic, backoff, graph dispatch
     job_tracker.py    # ingestion_jobs.steps JSONB update helper
     schemas.py        # IngestEvent, DeadLetterEvent Pydantic models
   src/db/repositories/
     ingestion_job_repository.py
   src/graphs/
     ingest_graph/
       __init__.py
       graph.py        # Stub (full impl: rag-engineer)
       state.py        # IngestGraphState TypedDict
   ```

2. **Implement `src/ingest/schemas.py`** — `IngestEvent` and `DeadLetterEvent` with `schema_version` validator

3. **Implement `src/db/repositories/ingestion_job_repository.py`** — `get_active_job`, `create`, `mark_*` methods

4. **Implement `src/ingest/job_tracker.py`** — `JobTracker` class for step-by-step JSONB updates

5. **Implement `src/ingest/event_processor.py`** — `EventProcessor` with retry logic, backoff formula, idempotency check, schema version check

6. **Implement `src/ingest/worker.py`** — main loop, consumer group setup, heartbeat, abandoned message recovery, DLQ routing, Prometheus metrics

7. **Create `src/graphs/ingest_graph/state.py`** — `IngestGraphState` TypedDict matching `docs/architecture.md §8`

8. **Create `src/graphs/ingest_graph/graph.py`** — stub `run_ingest_graph()` that raises `NotImplementedError`

9. **Create health server** for worker on port 9090:
   ```python
   # src/ingest/health_server.py
   from fastapi import FastAPI
   import uvicorn, asyncio

   health_app = FastAPI()

   @health_app.get("/health")
   async def health() -> dict:
       return {"status": "ok", "component": "ingest-worker"}

   async def run_health_server() -> None:
       config = uvicorn.Config(health_app, host="0.0.0.0", port=9090, log_level="warning")
       server = uvicorn.Server(config)
       await server.serve()
   ```

10. **Configure consumer group auto-creation** — `XGROUP CREATE ingest_events ingest_workers $ MKSTREAM` on first startup

11. **Add `MINIO_WEBHOOK_SECRET` and `INGEST_NODE_MAX_RETRIES` to `.env.example`** (already in Settings from TASK-001)

12. **Write tests** (see Tests section)

13. **Update `docker-compose.yml`** — `ingest-worker` service entry if not already present

14. **Run:** `ruff check --fix . && mypy src/ingest/ src/graphs/ && pytest -x -q tests/unit/test_ingest_worker.py`

## Security Checklist

- Worker runs with minimum required DB permissions (no DDL, no access to tables outside its scope)
- `IngestEvent` parsed with Pydantic — no raw dict access to Redis message fields in business logic
- `schema_version` unknown → immediately routed to DLQ without retries (prevents schema-version confusion attacks)
- `MaxRetriesExceededError` stores only safe metadata in DLQ — no document content, no PII
- Dead-letter event logged at ERROR level with `document_id` and `tenant_id` only — no file content
- Worker does NOT log document text, extracted content, validation_result, or pii_flags
- `tenant_id` and `document_id` in DLQ event are double-checked against DB before use
- Heartbeat Redis key uses worker_id (hostname+pid) — no sensitive information in key name
- DB sessions closed properly after each event (using `async with AsyncSessionLocal() as session`)
- Signal handlers (`SIGTERM`, `SIGINT`) stop the worker cleanly after finishing current event

## Terms of Use (relevant constraints)

- Per `docs/architecture.md §16`: DLQ alert must fire when `XLEN ingest_events_dlq > 0`. The Prometheus gauge `ingest_dlq_length` satisfies this when scraped by the `DlqNotEmpty` Grafana alert rule.
- Worker heartbeat `SET worker:heartbeat:{worker_id} EX 120` is mandatory. Missing heartbeat for > 120s triggers `WorkerHeartbeatMissing` alert (PagerDuty per §17 alert table).
- Ingest graph state (LangGraph checkpoint) is the authoritative record of where processing paused for `needs_review` documents. The `ingestion_jobs.langgraph_thread_id` is the link. Do NOT delete checkpoints for documents in `needs_review` status — the admin resume flow depends on them.
- Backoff formula is not negotiable: `min(base * 2^attempt + jitter, max_delay)` with jitter ±20%. Do not simplify to fixed delays.

## Tests

References TASK-016 `tests/unit/`, `tests/integration/`.

**`tests/unit/test_ingest_worker.py`**

- `compute_backoff(attempt=0)` → ~2s (within jitter range)
- `compute_backoff(attempt=5)` → capped at `MAX_DELAY_S=30s`
- `compute_backoff` values increase monotonically on average (jitter can vary)
- `IngestEvent` with `schema_version="2"` → `ValueError`
- `IngestEvent` with `schema_version="1"` and valid fields → parses correctly
- `EventProcessor.process()` with already-completed document (`status=ready`) → returns True without calling graph
- `EventProcessor.process()` with `retry_count >= MAX_RETRIES` → raises `MaxRetriesExceededError`
- Worker moves event to DLQ after `MAX_RETRIES` failures (mocked Redis and DB)
- DLQ event contains `original_event`, `failure_reason`, `failure_count`, `document_id`, `tenant_id`
- Document status updated to `failed` when moved to DLQ
- Prometheus counter `ingest_jobs_total{status="dlq"}` incremented on DLQ move

**`tests/unit/test_job_tracker.py`**

- `begin_stage("fetch")` → appends step with `status="running"` and `started_at`
- `complete_stage("fetch")` → updates last step `status="completed"`, `completed_at` set
- `fail_stage("fetch", "timeout")` → updates last step `status="failed"`, `retry_count` incremented
- Error text truncated at 2000 characters in `fail_stage`

**`tests/integration/test_ingest_pipeline.py`** (requires Redis + Postgres testcontainers)

- Full round-trip: publish event to `ingest_events` → worker consumes → graph stub raises `NotImplementedError` → retry → max retries → DLQ
- Consumer group `ingest_workers` created if not exists
- `XACK` called on successful processing (mocked graph returns successfully)
- Abandoned message (idle > 60s) is claimed by `XAUTOCLAIM`
- `ingestion_jobs.steps` contains correct stage records in order

**`tests/unit/test_ingest_schemas.py`**

- Unknown schema_version raises `ValueError` with clear message
- `document_id` not a valid UUID → `ValueError`
- All required fields present → `IngestEvent` instantiates correctly
- `DeadLetterEvent` serializes to dict for Redis `XADD`

**Heartbeat test:**

- Heartbeat loop writes `SET worker:heartbeat:{id} EX 120` every 30s (verify via mock Redis)
- Heartbeat Redis failure does NOT stop the worker (exception caught and logged)

## Definition of Done

- [ ] `src/ingest/worker.py` main loop with `XREADGROUP`, `XACK`, `XAUTOCLAIM` for recovery
- [ ] `IngestEvent` Pydantic model with `schema_version="1"` validation; unknown versions → DLQ immediately
- [ ] Exponential backoff formula matches `docs/architecture.md §16`: `min(base * 2^attempt * (1 + jitter), max_delay)`
- [ ] After `MAX_RETRIES` failures: event moved to `ingest_events_dlq`, document status set to `failed`
- [ ] `ingest_dlq_length` Prometheus gauge updated on every DLQ write
- [ ] Worker heartbeat: Redis key `SET worker:heartbeat:{worker_id} EX 120` every 30s
- [ ] `WORKER_HEARTBEAT` Prometheus gauge updated per heartbeat
- [ ] Idempotency: document already `status=ready` → `XACK` without reprocessing
- [ ] `ingestion_jobs.steps` JSONB array updated by `JobTracker` at each stage
- [ ] Health endpoint on port 9090 (`GET /health` returns 200)
- [ ] Metrics endpoint on port 9090 (`GET /metrics` in Prometheus format)
- [ ] `IngestGraphState` TypedDict defined in `src/graphs/ingest_graph/state.py`
- [ ] Signal handler for graceful shutdown (`SIGTERM`, `SIGINT`)
- [ ] All unit tests for worker, job tracker, and event schemas pass
- [ ] Integration test for full retry-to-DLQ flow passes
- [ ] `ruff check . && mypy src/ingest/ src/graphs/ingest_graph/` exit zero
