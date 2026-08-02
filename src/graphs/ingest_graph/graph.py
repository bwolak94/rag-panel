"""LangGraph ingest pipeline factory.

build_ingest_graph() compiles the ingest pipeline with optional Postgres checkpointer.
build_resume_graph() compiles a shortened pipeline (node_chunk → END) for admin-approved docs.
run_ingest_graph() is the entry point called by EventProcessor (backward-compatible).
resume_ingest_graph() is called by the router background task after admin approval.

Graph topology — standard (fixed — change requires ADR):
    node_fetch → node_extract → node_dedupe →[cond]→ node_validate →[cond]→ node_pii_scan
    →[cond]→ node_chunk → node_embed → node_upsert → node_persist → END

Graph topology — with Vision extraction (opt-in via chunk_config.vision_extraction_enabled):
    node_fetch → node_extract → node_dedupe →[cond]→ node_validate →[cond]→ node_pii_scan
    →[cond]→ node_chunk → node_extract_vision → node_embed → node_upsert → node_persist → END

Graph topology — with Graph RAG (opt-in via chunk_config.graph_rag_enabled):
    node_fetch → node_extract → node_dedupe →[cond]→ node_validate →[cond]→ node_pii_scan
    →[cond]→ node_chunk → node_extract_entities → node_embed → node_upsert → node_persist → END

Graph topology — with both Vision + Graph RAG enabled:
    node_fetch → node_extract → node_dedupe →[cond]→ node_validate →[cond]→ node_pii_scan
    →[cond]→ node_chunk → node_extract_vision → node_extract_entities
    → node_embed → node_upsert → node_persist → END

Resume graph topology (skip to chunking after admin approval):
    node_chunk → node_embed → node_upsert → node_persist → END

Per docs/architecture.md §8.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.llm_client import LLMClient
from src.core.clients.minio_client import get_minio_client
from src.db.models.ingestion_job import IngestionJob
from src.graphs.ingest_graph import nodes
from src.graphs.ingest_graph.routing import (
    route_after_dedupe,
    route_after_pii,
    route_after_semantic_dedup,
    route_after_validate,
)
from src.graphs.ingest_graph.state import IngestState
from src.ingest.schemas import IngestEvent

logger = structlog.get_logger(__name__)


def _fetch_extracted(minio_client: Any, bucket: str, key: str) -> bytes:
    """Fetch extracted JSON from MinIO in a thread-safe blocking call.

    Args:
        minio_client: Synchronous minio-py client instance.
        bucket: MinIO bucket name.
        key: Object key to fetch.

    Returns:
        Raw bytes of the object body.
    """
    response = minio_client.get_object(bucket, key)
    try:
        return response.read()  # type: ignore[no-any-return]
    finally:
        response.close()
        response.release_conn()


def build_ingest_graph(
    checkpointer: Any = None,
    graph_rag_enabled: bool = False,
    vision_extraction_enabled: bool = False,
    semantic_dedup_enabled: bool = False,
) -> Any:
    """Compile and return the ingest StateGraph.

    Args:
        checkpointer: Optional AsyncPostgresSaver for checkpoint-based graph resumption.
                      Pass None for testing or when langgraph-checkpoint-postgres is
                      not configured.
        graph_rag_enabled: When True, inserts node_extract_entities between node_chunk
                           (or node_extract_vision when both flags are set) and node_embed.
                           Controlled by collection.chunk_config["graph_rag_enabled"].
                           Defaults to False (standard pipeline).
        vision_extraction_enabled: When True, inserts node_extract_vision between
                                   node_chunk and node_embed (or node_extract_entities
                                   when graph_rag_enabled is also True).
                                   Controlled by
                                   collection.chunk_config["vision_extraction_enabled"].
                                   Defaults to False (standard pipeline).
        semantic_dedup_enabled: When True, inserts node_semantic_dedup between node_embed
                                and node_upsert. Detects near-duplicate documents via
                                cosine similarity on mean-pooled chunk embeddings.
                                Controlled by collection.chunk_config["semantic_dedup_enabled"].
                                Defaults to False (standard pipeline).

    Returns:
        Compiled LangGraph CompiledStateGraph.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(IngestState)  # type: ignore[type-arg]

    builder.add_node("node_fetch", nodes.node_fetch)  # type: ignore[call-overload]
    builder.add_node("node_extract", nodes.node_extract)  # type: ignore[call-overload]
    builder.add_node("node_dedupe", nodes.node_dedupe)  # type: ignore[call-overload]
    builder.add_node("node_validate", nodes.node_validate)  # type: ignore[call-overload]
    builder.add_node("node_pii_scan", nodes.node_pii_scan)  # type: ignore[call-overload]
    builder.add_node("node_chunk", nodes.node_chunk)  # type: ignore[call-overload]
    builder.add_node("node_embed", nodes.node_embed)  # type: ignore[call-overload]
    builder.add_node("node_upsert", nodes.node_upsert)  # type: ignore[call-overload]
    builder.add_node("node_persist", nodes.node_persist)  # type: ignore[call-overload]

    builder.set_entry_point("node_fetch")
    builder.add_edge("node_fetch", "node_extract")
    builder.add_edge("node_extract", "node_dedupe")

    builder.add_conditional_edges(
        "node_dedupe",
        route_after_dedupe,
        {"node_validate": "node_validate", END: END},
    )
    builder.add_conditional_edges(
        "node_validate",
        route_after_validate,
        {"node_pii_scan": "node_pii_scan", END: END},
    )
    builder.add_conditional_edges(
        "node_pii_scan",
        route_after_pii,
        {"node_chunk": "node_chunk", END: END},
    )

    # Wire together the optional enrichment nodes after node_chunk.
    # Topology variants (both flags are independent opt-ins):
    #   standard:             node_chunk → node_embed
    #   vision only:          node_chunk → node_extract_vision → node_embed
    #   graph_rag only:       node_chunk → node_extract_entities → node_embed
    #   vision + graph_rag:   node_chunk → node_extract_vision → node_extract_entities
    #                                    → node_embed
    if vision_extraction_enabled:
        builder.add_node(  # type: ignore[call-overload]
            "node_extract_vision", nodes.node_extract_vision
        )
        builder.add_edge("node_chunk", "node_extract_vision")
        if graph_rag_enabled:
            builder.add_node(  # type: ignore[call-overload]
                "node_extract_entities", nodes.node_extract_entities
            )
            builder.add_edge("node_extract_vision", "node_extract_entities")
            builder.add_edge("node_extract_entities", "node_embed")
        else:
            builder.add_edge("node_extract_vision", "node_embed")
    elif graph_rag_enabled:
        # Vision disabled, Graph RAG only: node_chunk → node_extract_entities → node_embed
        builder.add_node(  # type: ignore[call-overload]
            "node_extract_entities", nodes.node_extract_entities
        )
        builder.add_edge("node_chunk", "node_extract_entities")
        builder.add_edge("node_extract_entities", "node_embed")
    else:
        # Standard topology: node_chunk → node_embed
        builder.add_edge("node_chunk", "node_embed")

    # Semantic dedup runs after embed (needs embeddings) and before upsert.
    if semantic_dedup_enabled:
        builder.add_node(  # type: ignore[call-overload]
            "node_semantic_dedup", nodes.node_semantic_dedup
        )
        builder.add_edge("node_embed", "node_semantic_dedup")
        builder.add_conditional_edges(
            "node_semantic_dedup",
            route_after_semantic_dedup,
            {"node_upsert": "node_upsert", END: END},
        )
    else:
        builder.add_edge("node_embed", "node_upsert")

    builder.add_edge("node_upsert", "node_persist")
    builder.add_edge("node_persist", END)

    return builder.compile(checkpointer=checkpointer)


async def _resolve_pipeline_flags(
    session: AsyncSession,
    collection_id: uuid.UUID,
) -> tuple[bool, bool, bool]:
    """Read opt-in feature flags from collection.chunk_config.

    Returns (graph_rag_enabled, vision_extraction_enabled, semantic_dedup_enabled).
    All default to False when the collection is not found or the flag is absent/falsy.
    """
    from sqlalchemy import select

    from src.db.models.collection import Collection

    result = await session.execute(select(Collection).where(Collection.id == collection_id))
    collection = result.scalar_one_or_none()
    if collection is None:
        return False, False, False
    chunk_config: dict[str, Any] = collection.chunk_config or {}
    return (
        bool(chunk_config.get("graph_rag_enabled", False)),
        bool(chunk_config.get("vision_extraction_enabled", False)),
        bool(chunk_config.get("semantic_dedup_enabled", False)),
    )


async def run_ingest_graph(
    event: IngestEvent,
    job_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """Execute the ingest graph pipeline for a single document.

    Entry point called by EventProcessor. Creates a LangGraph thread_id,
    stores it in ingestion_jobs.langgraph_thread_id, and invokes the compiled graph.

    On success: document.status → 'ready', job.status → 'completed'.
    On needs_review: document.status → 'needs_review', job.status → 'awaiting_review',
        checkpoint preserved for admin resume flow.
    On failure: raises IngestNodeError; caller (EventProcessor) handles retry/DLQ.
    """
    thread_id = uuid.uuid4()

    # Store thread_id and mark job as processing
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(langgraph_thread_id=thread_id, status="processing")
    )
    await session.flush()

    graph_rag_enabled, vision_extraction_enabled, semantic_dedup_enabled = (
        await _resolve_pipeline_flags(session, event.collection_id)
    )
    graph = build_ingest_graph(
        graph_rag_enabled=graph_rag_enabled,
        vision_extraction_enabled=vision_extraction_enabled,
        semantic_dedup_enabled=semantic_dedup_enabled,
    )

    initial_state = IngestState(
        document_id=event.document_id,
        tenant_id=event.tenant_id,
        collection_id=event.collection_id,
        minio_key=event.minio_key,
        job_id=job_id,
    )

    config: dict[str, Any] = {
        "configurable": {
            "thread_id": str(thread_id),
            "db": session,
            "minio": get_minio_client(),
            "llm": LLMClient(),
        }
    }

    logger.info(
        "ingest_graph_started",
        document_id=str(event.document_id),
        tenant_id=str(event.tenant_id),
        job_id=str(job_id),
        thread_id=str(thread_id),
    )

    await graph.ainvoke(initial_state.model_dump(), config=config)

    logger.info(
        "ingest_graph_completed",
        document_id=str(event.document_id),
        job_id=str(job_id),
    )


def build_resume_graph(checkpointer: Any = None) -> Any:
    """Build a shortened ingest graph starting at node_chunk (for approved documents).

    Used when an admin approves a 'needs_review' document. The full extraction
    and validation steps are skipped — only chunking, embedding, upsert, and
    persist are executed. The caller must populate IngestState with
    extracted_text and extracted_sections before invoking.

    Args:
        checkpointer: Optional AsyncPostgresSaver.

    Returns:
        Compiled LangGraph CompiledStateGraph starting at node_chunk.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(IngestState)  # type: ignore[type-arg]
    builder.add_node("node_chunk", nodes.node_chunk)  # type: ignore[call-overload]
    builder.add_node("node_embed", nodes.node_embed)  # type: ignore[call-overload]
    builder.add_node("node_upsert", nodes.node_upsert)  # type: ignore[call-overload]
    builder.add_node("node_persist", nodes.node_persist)  # type: ignore[call-overload]
    builder.set_entry_point("node_chunk")
    builder.add_edge("node_chunk", "node_embed")
    builder.add_edge("node_embed", "node_upsert")
    builder.add_edge("node_upsert", "node_persist")
    builder.add_edge("node_persist", END)
    return builder.compile(checkpointer=checkpointer)


async def resume_ingest_graph(
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """Resume the ingest pipeline from node_chunk after admin approval.

    Reads extracted section metadata from MinIO (processed/{document_id}/extracted.json).
    Note: node_extract saves only section metadata (section_index, page, heading) without
    the full text. The extracted_text field will be empty if not cached; node_chunk will
    fall back to concatenating section texts or short-circuit gracefully.

    Runs the chunk→embed→upsert→persist sub-graph. Called by router background task after
    admin approval.

    Args:
        document_id: The approved document UUID.
        job_id: The IngestionJob UUID (for status updates and thread_id).
        tenant_id: The tenant UUID (used to scope DB queries — prevents cross-tenant access).
        session: Async DB session (caller-scoped; caller must commit after this returns).

    Raises:
        NotFoundError: If document or job not found for the given tenant.
    """
    import json as _json

    from src.core.exceptions import NotFoundError
    from src.db.models.document import Document
    from src.graphs.ingest_graph.state import Section

    # Load document scoped to tenant_id (prevents cross-tenant access)
    doc_row = (
        await session.execute(
            select(Document).where(
                Document.id == document_id,
                Document.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if doc_row is None:
        raise NotFoundError(f"Document {document_id} not found")

    # Load job scoped to tenant_id
    job_row = (
        await session.execute(
            select(IngestionJob).where(
                IngestionJob.id == job_id,
                IngestionJob.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if job_row is None:
        raise NotFoundError(f"Job {job_id} not found")

    # Load extracted section metadata from MinIO processed/ prefix
    minio = get_minio_client()
    extracted_key = f"processed/{document_id}/extracted.json"

    from src.db.repositories.tenant_repository import TenantRepository

    tenant = await TenantRepository(session).get_by_id(doc_row.tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found")
    bucket = f"tenant-{tenant.slug}"

    extracted_text: str = ""
    sections: list[Section] = []
    try:
        raw_data: bytes = await asyncio.to_thread(_fetch_extracted, minio, bucket, extracted_key)
        raw_sections: list[dict[str, Any]] = _json.loads(raw_data)
        # node_extract saves: [{section_index, page, heading}] — text is not persisted
        # Build Section objects from metadata; text will be empty (re-chunked from raw)
        sections = [
            Section(
                heading=s.get("heading"),
                text="",  # not stored in processed/ — will need raw document
                page=s.get("page"),
                section_index=s["section_index"],
            )
            for s in raw_sections
        ]
    except Exception as exc:
        logger.warning(
            "resume_ingest_graph.minio_read_failed",
            document_id=str(document_id),
            error=str(exc),
        )
        sections = []

    # Update job status to processing
    new_thread_id = uuid.uuid4()
    await session.execute(
        update(IngestionJob)
        .where(IngestionJob.id == job_id)
        .values(
            langgraph_thread_id=new_thread_id,
            status="processing",
            current_step="chunk",
        )
    )
    await session.flush()

    validation_result = None
    if doc_row.validation_result is not None:
        from src.graphs.ingest_graph.state import ValidationResult

        try:
            validation_result = ValidationResult.model_validate(doc_row.validation_result)
        except Exception as exc:
            logger.warning(
                "resume_ingest_graph.validation_result_parse_failed",
                document_id=str(document_id),
                error=str(exc),
            )
            validation_result = None

    initial_state = IngestState(
        document_id=document_id,
        tenant_id=doc_row.tenant_id,
        collection_id=doc_row.collection_id,
        minio_key=doc_row.minio_key,
        job_id=job_id,
        sha256=doc_row.sha256 or "",
        extracted_text=extracted_text if extracted_text else None,
        extracted_sections=sections if sections else None,
        validation_result=validation_result,
        status="indexing",
    )

    config: dict[str, Any] = {
        "configurable": {
            "thread_id": str(new_thread_id),
            "db": session,
            "minio": minio,
            "llm": LLMClient(),
        }
    }

    graph = build_resume_graph()
    await graph.ainvoke(initial_state.model_dump(), config=config)

    logger.info(
        "resume_ingest_graph.completed",
        document_id=str(document_id),
        job_id=str(job_id),
    )
