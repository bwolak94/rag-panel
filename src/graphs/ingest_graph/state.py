"""IngestGraphState — shared state for the LangGraph ingest pipeline.

Per docs/architecture.md §8. This TypedDict flows through all graph nodes:
    fetch_from_minio → extract_text → dedupe_check → llm_validate
    → pii_scan → chunk → embed → upsert_qdrant → persist_status
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict


class IngestGraphState(TypedDict, total=False):
    # Inputs (set by worker before calling run_ingest_graph)
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    collection_id: uuid.UUID
    job_id: uuid.UUID
    minio_bucket: str
    minio_key: str
    content_type: str

    # Populated by fetch_from_minio node
    raw_bytes: bytes

    # Populated by extract_text node
    extracted_text: str
    page_count: int | None

    # Populated by dedupe_check node
    is_duplicate: bool

    # Populated by llm_validate node
    validation_passed: bool
    validation_result: dict[str, Any]
    needs_review: bool

    # Populated by pii_scan node
    pii_flags: list[str]  # Category codes only — no actual PII content

    # Populated by chunk node
    chunks: list[dict[str, Any]]

    # Populated by embed node
    embeddings: list[list[float]]

    # Populated by upsert_qdrant node
    upserted_point_ids: list[str]

    # Errors
    error: str | None
    failed_stage: str | None
