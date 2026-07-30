"""
Ingest Worker — Redis Streams consumer.

Run with: python -m src.ingest.worker

Consumes from `ingest_events` stream using consumer groups (XREADGROUP).
Routes permanently failed events to `ingest_events_dlq` after MAX_RETRIES.
Publishes Prometheus metrics and a heartbeat key every 30 seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid
from datetime import UTC, datetime

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from src.core.clients.redis_client import get_redis_client
from src.core.database import AsyncSessionLocal
from src.core.logging import configure_logging
from src.ingest.event_processor import EventProcessor, MaxRetriesExceededError

logger = structlog.get_logger(__name__)

# Prometheus metrics
JOBS_TOTAL = Counter("ingest_jobs_total", "Total ingest jobs", ["status"])
JOB_DURATION = Histogram("ingest_job_duration_seconds", "Ingest job duration", ["status"])
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
        redis = await get_redis_client()

        # Ensure consumer group exists (MKSTREAM creates the stream if missing)
        with contextlib.suppress(Exception):
            await redis.xgroup_create(STREAM_NAME, CONSUMER_GROUP, id="$", mkstream=True)

        logger.info("worker_started", worker_id=self._worker_id)
        self._running = True
        await asyncio.gather(
            self._consume_loop(redis),
            self._heartbeat_loop(redis),
            self._recover_abandoned_messages(redis),
        )

    async def _consume_loop(self, redis: object) -> None:
        while self._running:
            try:
                messages = await redis.xreadgroup(  # type: ignore[attr-defined]
                    groupname=CONSUMER_GROUP,
                    consumername=self._worker_id,
                    streams={STREAM_NAME: ">"},
                    count=1,
                    block=BLOCK_MS,
                )
                for _stream, entries in messages or []:
                    for message_id, fields in entries:
                        await self._process_message(redis, message_id, fields)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("consume_loop_error", error=str(exc))
                await asyncio.sleep(1)

    async def _process_message(
        self,
        redis: object,
        message_id: str,
        fields: dict[str, str],
    ) -> None:
        start = datetime.now(UTC)
        document_id = fields.get("document_id", "unknown")

        try:
            async with AsyncSessionLocal() as session:
                success = await self._processor.process(session, fields)

            if success:
                await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)  # type: ignore[attr-defined]
                JOBS_TOTAL.labels(status="success").inc()
                duration = (datetime.now(UTC) - start).total_seconds()
                JOB_DURATION.labels(status="success").observe(duration)
                logger.info("ingest_event_acked", document_id=document_id)
            else:
                # Temporary failure — don't ACK; message stays in PEL for retry
                JOBS_TOTAL.labels(status="retry").inc()

        except MaxRetriesExceededError as exc:
            await self._move_to_dlq(redis, message_id, fields, str(exc), exc.failure_count)
            await redis.xack(STREAM_NAME, CONSUMER_GROUP, message_id)  # type: ignore[attr-defined]
            JOBS_TOTAL.labels(status="dlq").inc()
            logger.error(
                "event_moved_to_dlq",
                document_id=document_id,
                reason=str(exc),
            )

    async def _move_to_dlq(
        self,
        redis: object,
        message_id: str,
        original_fields: dict[str, str],
        reason: str,
        failure_count: int,
    ) -> None:
        dlq_entry = {
            "original_event": str(original_fields),
            "failure_reason": reason[:500],
            "failure_count": str(failure_count),
            "last_failed_at": datetime.now(UTC).isoformat(),
            "document_id": original_fields.get("document_id", ""),
            "tenant_id": original_fields.get("tenant_id", ""),
        }
        await redis.xadd(DLQ_STREAM, dlq_entry)  # type: ignore[attr-defined]

        # Update document status to failed in DB
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

        # Update Prometheus DLQ gauge
        dlq_len = await redis.xlen(DLQ_STREAM)  # type: ignore[attr-defined]
        DLQ_LENGTH.set(dlq_len)

    async def _heartbeat_loop(self, redis: object) -> None:
        """Writes heartbeat Redis key every 30 seconds. Missing for >120s triggers alert."""
        heartbeat_failures = 0
        parent_task: asyncio.Task[None] | None = None

        while self._running:
            if parent_task is None:
                parent_task = asyncio.current_task()

            try:
                ts = datetime.now(UTC).timestamp()
                await redis.set(  # type: ignore[attr-defined]
                    f"worker:heartbeat:{self._worker_id}",
                    str(ts),
                    ex=120,
                )
                WORKER_HEARTBEAT.labels(worker_id=self._worker_id).set(ts)
                heartbeat_failures = 0
            except Exception as exc:
                logger.error("heartbeat_error", error=str(exc), worker_id=self._worker_id)
                heartbeat_failures += 1
                if heartbeat_failures >= 3 and parent_task is not None:
                    parent_task.cancel()
            await asyncio.sleep(30)

    async def _recover_abandoned_messages(self, redis: object) -> None:
        """
        Periodically XAUTOCLAIM messages idle > 60s from crashed consumers.
        Per architecture §16: dead consumers' pending messages are reclaimed.
        """
        while self._running:
            await asyncio.sleep(60)
            try:
                claimed = await redis.xautoclaim(  # type: ignore[attr-defined]
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
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("recover_loop_error", error=str(exc))

    def stop(self) -> None:
        self._running = False


async def main() -> None:
    configure_logging()
    worker = IngestWorker()
    loop = asyncio.get_event_loop()

    def _shutdown(sig: signal.Signals) -> None:
        logger.info("shutdown_signal_received", signal=str(sig))
        worker.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown, sig)

    # Prometheus metrics exposed on port 9090 alongside the health endpoint
    start_http_server(9091)  # Use 9091 to avoid conflict with uvicorn on 9090

    from src.ingest.health_server import run_health_server

    await asyncio.gather(
        worker.start(),
        run_health_server(),
    )


if __name__ == "__main__":
    asyncio.run(main())
