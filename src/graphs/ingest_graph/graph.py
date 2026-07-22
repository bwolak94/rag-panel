"""LangGraph ingest pipeline factory.

build_ingest_graph() compiles the 9-node pipeline with optional Postgres checkpointer.
run_ingest_graph() is the entry point called by EventProcessor (backward-compatible).

Graph topology (fixed — change requires ADR):
    node_fetch → node_extract → node_dedupe →[cond]→ node_validate →[cond]→ node_pii_scan
    →[cond]→ node_chunk → node_embed → node_upsert → node_persist → END

Per docs/architecture.md §8.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.clients.llm_client import LLMClient
from src.core.clients.minio_client import get_minio_client
from src.db.models.ingestion_job import IngestionJob
from src.graphs.ingest_graph import nodes
from src.graphs.ingest_graph.routing import (
    route_after_dedupe,
    route_after_pii,
    route_after_validate,
)
from src.graphs.ingest_graph.state import IngestState
from src.ingest.schemas import IngestEvent

logger = structlog.get_logger(__name__)


def build_ingest_graph(checkpointer: Any = None) -> Any:
    """Compile and return the ingest StateGraph.

    Args:
        checkpointer: Optional AsyncPostgresSaver for checkpoint-based graph resumption.
                      Pass None for testing or when langgraph-checkpoint-postgres is
                      not configured.

    Returns:
        Compiled LangGraph CompiledStateGraph.
    """
    from langgraph.graph import END, StateGraph

    builder: StateGraph = StateGraph(IngestState)

    builder.add_node("node_fetch", nodes.node_fetch)
    builder.add_node("node_extract", nodes.node_extract)
    builder.add_node("node_dedupe", nodes.node_dedupe)
    builder.add_node("node_validate", nodes.node_validate)
    builder.add_node("node_pii_scan", nodes.node_pii_scan)
    builder.add_node("node_chunk", nodes.node_chunk)
    builder.add_node("node_embed", nodes.node_embed)
    builder.add_node("node_upsert", nodes.node_upsert)
    builder.add_node("node_persist", nodes.node_persist)

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

    builder.add_edge("node_chunk", "node_embed")
    builder.add_edge("node_embed", "node_upsert")
    builder.add_edge("node_upsert", "node_persist")
    builder.add_edge("node_persist", END)

    return builder.compile(checkpointer=checkpointer)


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
    await session.commit()

    graph = build_ingest_graph()

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
