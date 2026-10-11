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
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import IngestNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.db.models.audit_log import AuditLog
from src.db.models.chunks_registry import ChunksRegistry
from src.db.models.document import Document
from src.db.models.ingestion_job import IngestionJob
from src.db.repositories.entity_repository import EntityRepository
from src.graphs.ingest_graph.helpers import update_step, utcnow
from src.graphs.ingest_graph.state import IngestState
from src.retrieval.service import RetrievalService

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
    retrieval: RetrievalService | None = cfg.get("retrieval")
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

        # ----------------------------------------------------------------
        # Graph RAG: persist MedicalEntity and EntityRelation rows in the
        # same transaction as chunks_registry + document status so either
        # everything lands atomically or nothing does.
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

        # ----------------------------------------------------------------
        # ADR-024: Version promotion — Postgres part (must be in transaction).
        # Sets is_current=False on the previous version's document row.
        # The Qdrant part runs AFTER commit to prevent a CancelledError during
        # the Qdrant network call from preventing session.commit() from running.
        # ----------------------------------------------------------------
        if state.previous_version_id is not None:
            if retrieval is None:
                raise IngestNodeError(
                    "previous_version_id is set but 'retrieval' is not in config — "
                    "version demotion requires RetrievalService"
                )
            await _demote_previous_version_postgres(session=session, state=state)

        await session.commit()

        # ----------------------------------------------------------------
        # ADR-024: Qdrant version demotion — best-effort, AFTER commit.
        # Runs outside the transaction so a Qdrant error never rolls back the
        # Postgres commit. Qdrant is eventually consistent with Postgres here.
        # ----------------------------------------------------------------
        if state.previous_version_id is not None and retrieval is not None:
            await _demote_previous_version_qdrant(
                session=session,
                retrieval=retrieval,
                state=state,
            )

        version_promoted = state.previous_version_id is not None
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
                "version_promoted": version_promoted,
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
                "version_promoted": version_promoted,
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


async def _demote_previous_version_postgres(
    session: AsyncSession,
    state: IngestState,
) -> None:
    """Set is_current=False on the previous version's Postgres document row.

    Runs INSIDE the caller's transaction — committed atomically with the rest
    of node_persist's writes.  The Qdrant counterpart must run AFTER commit
    (see _demote_previous_version_qdrant).

    Args:
        session: Active AsyncSession (transaction owned by the caller).
        state: Current IngestState — provides tenant_id and previous_version_id.
    """
    prev_id = state.previous_version_id
    if prev_id is None:
        return

    await session.execute(
        update(Document)
        .where(
            Document.id == prev_id,
            Document.tenant_id == state.tenant_id,
        )
        .values(is_current=False)
    )
    logger.info(
        "node_persist.version_demoted_postgres",
        previous_version_id=str(prev_id),
        document_id=str(state.document_id),
    )


async def _demote_previous_version_qdrant(
    session: AsyncSession,
    retrieval: RetrievalService,
    state: IngestState,
) -> None:
    """Update is_current=False payload on the previous version's Qdrant points.

    Runs AFTER session.commit() — best-effort.  Errors are logged but never
    re-raised; Qdrant is eventually consistent with Postgres (the source of truth
    for is_current).

    Args:
        session: AsyncSession — used only for the model-name lookup query.
        retrieval: RetrievalService (required — caller must validate non-None).
        state: Current IngestState — provides tenant_id, previous_version_id, collection_id.
    """
    prev_id = state.previous_version_id
    if prev_id is None:
        return

    try:
        from sqlalchemy import select

        from src.db.models.collection import Collection
        from src.db.models.models_registry import ModelsRegistry
        from src.retrieval.schemas import TenantContext

        model_name_q = (
            select(ModelsRegistry.name)
            .join(Collection, Collection.embedding_model_id == ModelsRegistry.id)
            .where(
                Collection.id == state.collection_id,
                Collection.tenant_id == state.tenant_id,
            )
        )
        model_name: str | None = (await session.execute(model_name_q)).scalar_one_or_none()
        if model_name is None:
            logger.warning(
                "node_persist.version_demote_qdrant_skipped_no_model",
                previous_version_id=str(prev_id),
                collection_id=str(state.collection_id),
            )
            return

        model_slug = model_name.lower().replace("-", "_").replace(" ", "_")
        qdrant_collection = f"emb_{model_slug}"
        ctx = TenantContext(
            tenant_id=state.tenant_id,
            allowed_collection_ids=[state.collection_id],
        )
        await retrieval.update_payload(
            ctx=ctx,
            qdrant_collection=qdrant_collection,
            document_id=prev_id,
            payload_patch={"is_current": False},
        )
        logger.info(
            "node_persist.version_demoted_qdrant",
            previous_version_id=str(prev_id),
            qdrant_collection=qdrant_collection,
        )
    except Exception as exc:
        # Best-effort: Qdrant failure must not block the ingest success path.
        logger.warning(
            "node_persist.version_demote_qdrant_failed",
            previous_version_id=str(prev_id),
            error_type=type(exc).__name__,
        )


async def _persist_entities(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    extracted_entities: list[dict[str, Any]],
) -> tuple[int, int]:
    """Persist MedicalEntity and EntityRelation rows extracted by node_extract_entities.

    Delegates to EntityRepository for idempotent upserts (ON CONFLICT DO NOTHING
    on the natural unique key).  Does NOT commit — the caller (node_persist) owns
    the transaction boundary and commits everything atomically.

    Tenant isolation: tenant_id is always written explicitly from the pipeline state;
    it is never read from the LLM output.

    Args:
        session: Active AsyncSession (transaction owned by the caller).
        tenant_id: The owning tenant UUID from pipeline state.
        document_id: The document UUID from pipeline state.
        extracted_entities: List of {"entities": [...], "relations": [...]} dicts.

    Returns:
        Tuple of (entities_saved, relations_saved) — counts of rows submitted.
    """
    # Flatten all entities and relations across batches
    all_entities: list[dict[str, Any]] = []
    all_relations: list[dict[str, Any]] = []
    for batch in extracted_entities:
        all_entities.extend(batch.get("entities", []))
        all_relations.extend(batch.get("relations", []))

    if not all_entities:
        return 0, 0

    repo = EntityRepository(session)

    entities_saved = await repo.upsert_entities(
        tenant_id=tenant_id,
        document_id=document_id,
        entities=all_entities,
    )

    canonical_names = list({e["name"] for e in all_entities})
    name_to_id = await repo.resolve_name_to_id(
        tenant_id=tenant_id,
        document_id=document_id,
        canonical_names=canonical_names,
    )

    relations_saved = await repo.upsert_relations(
        tenant_id=tenant_id,
        document_id=document_id,
        relations=all_relations,
        name_to_id=name_to_id,
    )

    logger.info(
        "node_persist.entities_persisted",
        document_id=str(document_id),
        entities_saved=entities_saved,
        relations_saved=relations_saved,
    )
    return entities_saved, relations_saved


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(UTC) - start).total_seconds() * 1000)
