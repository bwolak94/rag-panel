"""Pydantic schemas for admin document review panel (TASK-012).

Security: llm_raw_response and rejection reasons are NOT exposed via API.
presigned URLs are returned but must not be logged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    code: str = Field(..., description="LOW_QUALITY | PII_DETECTED | UNKNOWN_TYPE | LOW_CONFIDENCE")
    message: str


class ValidationResultSchema(BaseModel):
    detected_type: str | None = None
    category: str | None = None
    quality_score: float | None = Field(None, ge=0.0, le=1.0)
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    pii_flags: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)


class IngestionStep(BaseModel):
    stage: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ReviewQueueItem(BaseModel):
    id: uuid.UUID
    filename: str
    collection_id: uuid.UUID
    collection_name: str
    uploaded_by_id: uuid.UUID | None
    uploaded_by_name: str
    uploaded_at: datetime
    validation_result: ValidationResultSchema
    flag_summary: str


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItem]
    total_count: int
    next_cursor: str | None = None
    has_next_page: bool


class DocumentReviewDetail(BaseModel):
    id: uuid.UUID
    filename: str
    collection_id: uuid.UUID
    collection_name: str
    uploaded_by_id: uuid.UUID | None
    uploaded_by_name: str
    uploaded_at: datetime
    size_bytes: int
    mime_type: str
    validation_result: ValidationResultSchema
    ingestion_steps: list[IngestionStep]
    ingestion_job_id: uuid.UUID
    preview_url: str = Field(..., description="Presigned MinIO GET URL, TTL=300s")
    preview_url_expires_at: datetime


class IngestionJobDetail(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    current_step: str | None
    retry_count: int
    steps: list[IngestionStep]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class ApproveDocumentRequest(BaseModel):
    note: str | None = Field(None, max_length=500)


class RejectDocumentRequest(BaseModel):
    reason: str = Field(..., min_length=10, max_length=1000)


class ReviewDecisionResponse(BaseModel):
    document_id: uuid.UUID
    new_status: str
    decided_at: datetime
    decided_by_id: uuid.UUID
    message: str
