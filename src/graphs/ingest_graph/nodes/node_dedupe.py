"""node_dedupe — SHA-256 deduplication within a tenant.

Deduplication is scoped to a single tenant (cross-tenant duplicates allowed).
If a duplicate is found, the document is marked 'rejected' and the graph halts.

Security:
- Only compares sha256 values — never document content.
- The existing_document_id in meta is an internal UUID, not PII.
"""

from __future__ import annotations

from typing import Any

import structlog
from langfuse import observe
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.document import Document
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)


@observe(name="node_dedupe", capture_input=False, capture_output=False)
async def node_dedupe(state: IngestState, config: dict[str, Any]) -> dict[str, Any]:
    """Check (tenant_id, sha256) for duplicates; halt on match.

    Args:
        state: Must have sha256 set by node_fetch.
        config: RunnableConfig with configurable["db"].

    Returns:
        {} if unique; {"status": "rejected", "halt": True} if duplicate.
    """
    cfg = config.get("configurable", {})
    session: AsyncSession = cfg["db"]
    step_start = utcnow()

    existing_id_result = await session.execute(
        select(Document.id).where(
            Document.tenant_id == state.tenant_id,
            Document.sha256 == state.sha256,
            Document.id != state.document_id,
            Document.status != "deleted",
        )
    )
    existing_id = existing_id_result.scalar_one_or_none()

    if existing_id is not None:
        await session.execute(
            update(Document)
            .where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
            .values(status="rejected")
        )
        await session.commit()

        await update_step(
            session,
            state.job_id,
            stage="dedupe",
            status="completed",
            started_at=step_start,
            meta={"result": "duplicate", "existing_document_id": str(existing_id)},
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "result": "duplicate",
                # existing_document_id is an internal UUID — not PII
                "existing_document_id": str(existing_id),
            }
        )
        logger.info(
            "node_dedupe_duplicate_found",
            document_id=str(state.document_id),
            existing_document_id=str(existing_id),
        )
        return {"status": "rejected", "halt": True}

    await update_step(
        session,
        state.job_id,
        stage="dedupe",
        status="completed",
        started_at=step_start,
        meta={"result": "unique"},
    )
    _lf_update_span(
        metadata={
            "document_id": str(state.document_id),
            "tenant_id": str(state.tenant_id),
            "result": "unique",
        }
    )
    return {}
