"""IngestState — Pydantic BaseModel for the LangGraph ingest pipeline.

Per docs/architecture.md §8 and TASK-008 specification.
All intermediate fields are Optional to allow partial state in checkpoints.

GDPR rules:
- raw_bytes must never appear in logs (use len(raw_bytes) for size).
- validation_result contains extracted content — never log it.
- pii_flags contains category labels only, never extracted PII values.
"""

from __future__ import annotations

import enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentStatus(enum.StrEnum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    NEEDS_REVIEW = "needs_review"
    INDEXING = "indexing"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"


class Section(BaseModel):
    heading: str | None = None
    text: str
    page: int | None = None
    section_index: int
    # Vision extraction fields — only set for image/table sections extracted by Docling.
    # section_type defaults to "text"; set to "image" or "table" for visual content.
    # image_b64 holds the base64-encoded PNG/JPEG for vision model input (GDPR: never log).
    section_type: str = "text"  # "text" | "image" | "table" | "diagram"
    image_b64: str | None = None  # GDPR: never log — contains document visual content


class ValidationResult(BaseModel):
    category: str | None = None
    confidence: float | None = None
    quality_score: float | None = None
    document_type: str | None = None
    language: str | None = None
    pii_flags: list[str] = Field(default_factory=list)  # type labels only, never values
    reasons: list[str] = Field(default_factory=list)


class ChunkData(BaseModel):
    chunk_index: int
    text: str
    page: int | None = None
    section: str | None = None
    token_count: int
    point_id: UUID  # SHA-256-derived deterministic UUID


class IngestState(BaseModel):
    # Identity — set by worker, never mutated by nodes
    document_id: UUID
    tenant_id: UUID
    collection_id: UUID
    minio_key: str
    job_id: UUID
    sha256: str = ""  # populated by node_fetch

    # Intermediate pipeline artifacts
    raw_bytes: bytes | None = None  # GDPR: never log content; clear after extract
    extracted_text: str | None = None  # GDPR: never log content
    extracted_sections: list[Section] | None = None
    validation_result: ValidationResult | None = None
    chunks: list[ChunkData] | None = None
    embeddings: list[list[float]] | None = None
    point_ids: list[UUID] | None = None
    # Graph RAG enrichment — set by node_extract_entities (opt-in per collection).
    # Each element is {"entities": [...], "relations": [...]} from one extraction batch.
    # None when graph_rag_enabled=False or extraction was skipped/failed.
    # GDPR: list contents are derived from document text — never log values.
    extracted_entities: list[dict[str, Any]] | None = None

    # Vision extraction — set by node_extract_vision (opt-in per collection).
    # Counts how many vision-derived ChunkData items were appended to state.chunks.
    # 0 when vision_extraction_enabled=False or no image/table sections were present.
    vision_chunks_count: int = 0

    # Semantic dedup — set by node_semantic_dedup (TASK-029)
    dedup_similarity: float | None = None  # cosine similarity to most similar doc
    dedup_similar_doc_id: UUID | None = None  # UUID of most similar existing document

    # Pipeline control
    status: str = "uploaded"
    error: str | None = None
    retry_count: int = 0
    halt: bool = False  # True → route to END (duplicate / needs_review)
