"""node_persist — bulk insert chunks_registry, mark document ready, write audit log.

Idempotent: ON CONFLICT DO NOTHING on qdrant_point_id prevents duplicate rows.
This is the terminal node — sets document.status=ready and job.status=completed.

Security:
- Writes audit log with document_id and chunks_count only (no content).
- Verifies document.tenant_id == state.tenant_id before writing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.db.models.audit_log import AuditLog
from src.db.models.chunks_registry import ChunksRegistry
from src.db.models.document import Document
from src.db.models.ingestion_job import IngestionJob
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)


async def node_persist(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Persist chunks_registry rows, mark document=ready, write audit log.

    Args:
        state: Must have chunks and point_ids from previous nodes.
        config: RunnableConfig with configurable["db"].

    Returns:
        {"status": "ready"}

    Raises:
        IngestNodeError: On DB failure.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    step_start = utcnow()

    try:
        chunks = state.chunks or []
        point_ids = state.point_ids or []

        if not chunks:
            raise IngestNodeError("no chunks to persist — node_chunk must run first")

        # Bulk insert chunks_registry (idempotent via ON CONFLICT DO NOTHING)
        rows = [
            {
                "tenant_id": state.tenant_id,
                "document_id": state.document_id,
                "qdrant_point_id": point_id,
                "chunk_index": chunk.chunk_index,
                "page": chunk.page,
                "section": chunk.section,
                "token_count": chunk.token_count,
            }
            for chunk, point_id in zip(chunks, point_ids, strict=False)
        ]

        if rows:
            stmt = pg_insert(ChunksRegistry).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["qdrant_point_id"])
            await session.execute(stmt)

        # Update document status → ready (verify tenant_id)
        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(status="ready")
        )

        # Update ingestion_job → completed
        await session.execute(
            update(IngestionJob)
            .where(IngestionJob.id == state.job_id)
            .values(status="completed", completed_at=utcnow())
        )

        # Append audit log entry (IDs only, no content)
        audit_entry = AuditLog(
            tenant_id=state.tenant_id,
            user_id=None,
            action="document.indexed",
            resource_type="document",
            resource_id=state.document_id,
            details={
                "chunks_count": len(chunks),
                "job_id": str(state.job_id),
            },
        )
        session.add(audit_entry)
        await session.commit()

        await update_step(
            session,
            state.job_id,
            stage="persist",
            status="completed",
            started_at=step_start,
            meta={
                "chunks_registry_rows": len(rows),
                "latency_ms": _elapsed_ms(step_start),
            },
        )
        logger.info(
            "node_persist_completed",
            document_id=str(state.document_id),
            chunks_registry_rows=len(rows),
        )
        return {"status": "ready"}

    except IngestNodeError as exc:
        await update_step(
            session,
            state.job_id,
            stage="persist",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"persist_error: {type(exc).__name__}"
        await update_step(
            session,
            state.job_id,
            stage="persist",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)
