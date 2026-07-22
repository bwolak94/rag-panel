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

    # Pipeline control
    status: str = "uploaded"
    error: str | None = None
    retry_count: int = 0
    halt: bool = False  # True → route to END (duplicate / needs_review)
