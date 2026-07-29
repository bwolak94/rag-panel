"""Pydantic schemas for Document upload and retrieval."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Canonical document status values
DocumentStatus = Literal[
    "uploaded", "validating", "needs_review", "indexing",
    "ready", "rejected", "failed", "deleted"
]


class DocumentUploadRequest(BaseModel):
    """
    Client-provided metadata before upload. Client computes SHA-256 locally
    so the API can deduplicate before the actual file transfer.
    """

    collection_id: uuid.UUID
    filename: str = Field(..., min_length=1, max_length=500)
    mime_type: str = Field(..., max_length=100)
    size_bytes: int = Field(..., gt=0)
    sha256: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    title: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("filename")
    @classmethod
    def sanitize_filename(cls, v: str) -> str:
        """Remove path traversal characters, null bytes, and reserved names."""
        # Strip directory components
        filename = v.split("/")[-1].split("\\")[-1]
        # Remove null bytes and control characters
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
        # Replace characters unsafe in object storage paths
        filename = re.sub(r'[<>:"|?*]', "_", filename)
        if not filename or filename in (".", ".."):
            raise ValueError("Invalid filename")
        return filename

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, v: str) -> str:
        from src.db.repositories.document_repository import ALLOWED_MIME_TYPES

        if v not in ALLOWED_MIME_TYPES:
            raise ValueError(
                f"MIME type '{v}' is not allowed. Allowed: {sorted(ALLOWED_MIME_TYPES)}"
            )
        return v

    @field_validator("size_bytes")
    @classmethod
    def validate_size(cls, v: int) -> int:
        from src.db.repositories.document_repository import MAX_SIZE_BYTES

        if v > MAX_SIZE_BYTES:
            raise ValueError(f"File size exceeds maximum of {MAX_SIZE_BYTES // 1024**2} MB")
        return v


class DocumentUploadResponse(BaseModel):
    """Returned to client; client uses upload_url for direct PUT to MinIO."""

    document_id: uuid.UUID
    job_id: uuid.UUID
    upload_url: str  # Presigned PUT URL
    minio_key: str  # Object path (informational)
    expires_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    collection_id: uuid.UUID
    title: str
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    category: str | None
    tags: list[str]
    language: str | None
    uploaded_by: uuid.UUID | None
    validation_result: dict[str, object] | None  # Returned only to admins with documents:manage
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class ReviewDecision(BaseModel):
    """Request body for admin document review decision."""

    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=2000)


class ReviewQueueResponse(BaseModel):
    """Paginated list of documents awaiting admin review."""

    items: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class MinIOWebhookEvent(BaseModel):
    """
    Schema for MinIO bucket notification. MinIO sends this as POST JSON.
    See docs/architecture.md §15 for canonical event schema.
    """

    EventName: str  # e.g., "s3:ObjectCreated:Put"
    Key: str  # e.g., "tenant-clinic/raw/coll-id/doc-id/file.pdf"
    Records: list[dict[str, object]]  # Raw S3-compatible notification records
