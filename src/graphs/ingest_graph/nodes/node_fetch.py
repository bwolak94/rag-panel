"""node_fetch — download document from MinIO and verify SHA-256 integrity.

Security:
- Always reads minio_key from the Document record in DB (never reconstructs it).
- Verifies SHA-256 against documents.sha256 stored at upload time.
- Never logs raw_bytes content; uses len() for size.
- Raises IngestNodeError (never bare Exception).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.db.models.document import Document
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)


async def node_fetch(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Download document bytes from MinIO; verify SHA-256 integrity.

    Args:
        state: Current ingest graph state.
        config: LangGraph RunnableConfig with configurable["db"] and configurable["minio"].

    Returns:
        {"raw_bytes": bytes, "sha256": str}

    Raises:
        IngestNodeError: On MinIO errors, document not found, or SHA-256 mismatch.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    minio = cfg["minio"]
    step_start = utcnow()

    try:
        # Always read minio_key from the document record — never reconstruct it.
        result = await session.execute(
            select(Document).where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
        )
        document = result.scalar_one_or_none()
        if document is None:
            raise IngestNodeError(
                f"Document {state.document_id} not found for tenant {state.tenant_id}"
            )

        bucket = f"tenant-{state.tenant_id}"

        def _get_object() -> bytes:
            resp = minio.get_object(bucket, document.minio_key)
            try:
                return bytes(resp.read())
            finally:
                resp.close()
                resp.release_conn()

        loop = asyncio.get_running_loop()
        raw_bytes: bytes = await loop.run_in_executor(None, _get_object)

        computed_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        if document.sha256 and computed_sha256 != document.sha256:
            raise IngestNodeError(
                f"SHA-256 mismatch for document {state.document_id}: "
                f"expected={document.sha256[:8]}…, got={computed_sha256[:8]}…"
            )

        await update_step(
            session,
            state.job_id,
            stage="fetch",
            status="completed",
            started_at=step_start,
            meta={"size_bytes": len(raw_bytes), "latency_ms": _elapsed_ms(step_start)},
        )
        logger.info(
            "node_fetch_completed",
            document_id=str(state.document_id),
            size_bytes=len(raw_bytes),
        )
        return {"raw_bytes": raw_bytes, "sha256": computed_sha256}

    except IngestNodeError as exc:
        await update_step(
            session,
            state.job_id,
            stage="fetch",
            status="failed",
            started_at=step_start,
            error=str(exc),
        )
        raise
    except Exception as exc:
        error_msg = f"fetch_error: {type(exc).__name__}"
        await update_step(
            session,
            state.job_id,
            stage="fetch",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)
