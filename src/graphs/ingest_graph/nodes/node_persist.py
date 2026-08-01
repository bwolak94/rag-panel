"""node_persist — bulk insert chunks_registry, mark document ready, write audit log.

Idempotent: ON CONFLICT DO NOTHING on qdrant_point_id prevents duplicate rows.
This is the terminal node — sets document.status=ready and job.status=completed.

Graph RAG:
When state.extracted_entities is not None, persists MedicalEntity and
EntityRelation rows to Postgres after the chunks_registry insert.  Entity
persistence uses ON CONFLICT DO NOTHING on (document_id, canonical_name,
entity_type) to remain idempotent on retries.

Security:
- Writes audit log with document_id and chunks_count only (no content).
- Verifies document.tenant_id == state.tenant_id before writing.
- tenant_id is always set on MedicalEntity and EntityRelation rows.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from langfuse import observe
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.audit_log import AuditLog
from src.db.models.chunks_registry import ChunksRegistry
from src.db.models.document import Document
from src.db.models.ingestion_job import IngestionJob
from src.db.models.knowledge_graph import EntityRelation, MedicalEntity
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState

logger = structlog.get_logger(__name__)


@observe(name="node_persist", capture_input=False, capture_output=False)
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

        # ----------------------------------------------------------------
        # Graph RAG: persist MedicalEntity and EntityRelation rows
        # ----------------------------------------------------------------
        entities_saved = 0
        relations_saved = 0
        if state.extracted_entities is not None:
            entities_saved, relations_saved = await _persist_entities(
                session=session,
                tenant_id=state.tenant_id,
                document_id=state.document_id,
                extracted_entities=state.extracted_entities,
            )

        elapsed = _elapsed_ms(step_start)
        await update_step(
            session,
            state.job_id,
            stage="persist",
            status="completed",
            started_at=step_start,
            meta={
                "chunks_registry_rows": len(rows),
                "entities_saved": entities_saved,
                "relations_saved": relations_saved,
                "latency_ms": elapsed,
            },
        )
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "job_id": str(state.job_id),
                "chunks_registry_rows": len(rows),
                "entities_saved": entities_saved,
                "relations_saved": relations_saved,
                "latency_ms": elapsed,
            }
        )
        # Invalidate retrieval cache for this collection so that subsequent queries
        # reflect the newly indexed document. Errors are swallowed — cache failure
        # must never block the ingest success path.
        try:
            from src.core.cache import get_rag_cache

            cache = get_rag_cache()
            await cache.invalidate_collection(state.tenant_id, state.collection_id)
        except Exception as cache_exc:
            logger.warning(
                "node_persist.cache_invalidation_failed",
                document_id=str(state.document_id),
                error=type(cache_exc).__name__,
            )

        logger.info(
            "node_persist_completed",
            document_id=str(state.document_id),
            chunks_registry_rows=len(rows),
            entities_saved=entities_saved,
            relations_saved=relations_saved,
        )
        return {"status": "ready"}

    except IngestNodeError as exc:
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
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
        _lf_update_span(
            metadata={
                "document_id": str(state.document_id),
                "tenant_id": str(state.tenant_id),
                "error": True,
                "error_type": type(exc).__name__,
            }
        )
        await update_step(
            session,
            state.job_id,
            stage="persist",
            status="failed",
            started_at=step_start,
            error=error_msg,
        )
        raise IngestNodeError(error_msg) from exc


async def _persist_entities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    extracted_entities: list[dict[str, Any]],
) -> tuple[int, int]:
    """Persist MedicalEntity and EntityRelation rows extracted by node_extract_entities.

    Idempotent: inserts are wrapped in ON CONFLICT DO NOTHING.
    The conflict target for MedicalEntity is (document_id, canonical_name, entity_type)
    so that re-ingesting the same document does not create duplicate entity rows.

    Tenant isolation: tenant_id is always written explicitly from the pipeline state;
    it is never read from the LLM output.

    Args:
        session: Active AsyncSession.
        tenant_id: The owning tenant UUID from pipeline state.
        document_id: The document UUID from pipeline state.
        extracted_entities: List of {"entities": [...], "relations": [...]} dicts.

    Returns:
        Tuple of (entities_saved, relations_saved) — counts of inserted rows.
    """
    # Flatten all entities and relations across batches
    all_entities: list[dict[str, Any]] = []
    all_relations: list[dict[str, Any]] = []
    for batch in extracted_entities:
        all_entities.extend(batch.get("entities", []))
        all_relations.extend(batch.get("relations", []))

    if not all_entities:
        return 0, 0

    # --- Insert MedicalEntity rows ---
    entity_rows: list[dict[str, Any]] = [
        {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "document_version_id": None,
            "entity_type": e["type"],
            "canonical_name": e["name"],
            "surface_forms": e.get("surface_forms", [e["name"]]),
            "icd_code": e.get("icd_code"),
            "atc_code": e.get("atc_code"),
            "confidence": e.get("confidence", 0.0),
            "chunk_ids": e.get("chunk_ids", []),
        }
        for e in all_entities
    ]

    entity_stmt = pg_insert(MedicalEntity).values(entity_rows)
    # Idempotency: skip duplicates on the natural key within this document
    entity_stmt = entity_stmt.on_conflict_do_nothing()
    await session.execute(entity_stmt)
    await session.flush()

    entities_saved = len(entity_rows)

    if not all_relations:
        await session.commit()
        return entities_saved, 0

    # --- Resolve entity name → DB id map for FK population ---
    # Re-query the rows we just inserted (or that already existed) scoped to this document
    # so we can map canonical_name to the real UUID primary key.
    canonical_names = list({e["name"] for e in all_entities})
    result = await session.execute(
        select(MedicalEntity.id, MedicalEntity.canonical_name).where(
            MedicalEntity.document_id == document_id,
            MedicalEntity.tenant_id == tenant_id,
            MedicalEntity.canonical_name.in_(canonical_names),
        )
    )
    name_to_id: dict[str, uuid.UUID] = {row.canonical_name: row.id for row in result}

    # --- Insert EntityRelation rows ---
    relation_rows: list[dict[str, Any]] = []
    for rel in all_relations:
        source_id = name_to_id.get(rel.get("source_name", ""))
        target_id = name_to_id.get(rel.get("target_name", ""))
        if source_id is None or target_id is None:
            # Entity not found in DB — skip this relation (defensive)
            continue
        relation_rows.append(
            {
                "tenant_id": tenant_id,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relation_type": rel["relation_type"],
                "confidence": rel.get("confidence", 0.0),
                "document_id": document_id,
                "evidence_text_hash": rel["evidence_text_hash"],
            }
        )

    relations_saved = 0
    if relation_rows:
        relation_stmt = pg_insert(EntityRelation).values(relation_rows)
        relation_stmt = relation_stmt.on_conflict_do_nothing()
        await session.execute(relation_stmt)
        relations_saved = len(relation_rows)

    await session.commit()

    logger.info(
        "node_persist.entities_persisted",
        document_id=str(document_id),
        entities_saved=entities_saved,
        relations_saved=relations_saved,
    )
    return entities_saved, relations_saved


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)
