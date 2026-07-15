"""
LangGraph ingest graph entry point.

Nodes (per docs/architecture.md §8):
    fetch_from_minio → extract_text → dedupe_check → llm_validate
    → pii_scan → chunk → embed → upsert_qdrant → persist_status

Full node implementation is the responsibility of rag-engineer (TASK-008+).
This stub provides the callable interface used by the ingest worker.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.ingest.schemas import IngestEvent


async def run_ingest_graph(
    event: IngestEvent,
    job_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """
    Executes the ingest LangGraph pipeline for a single document.

    Each node updates ingestion_jobs.steps via JobTracker.
    Graph state is checkpointed to Postgres via LangGraph checkpointer.
    langgraph_thread_id is stored in ingestion_jobs.langgraph_thread_id.

    On success: document.status → 'ready', job.status → 'completed'.
    On needs_review: document.status → 'needs_review', job.status → 'awaiting_review',
        checkpoint preserved for admin resume flow.
    On failure: raises exception; caller (EventProcessor) handles retry/DLQ.
    """
    raise NotImplementedError(
        "Ingest graph not yet implemented. See TASK-008 and beyond."
    )
