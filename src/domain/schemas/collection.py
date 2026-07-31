"""Pydantic schemas for the Collections API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ChunkConfig(BaseModel):
    """Validated schema for collections.chunk_config JSONB."""

    strategy: Literal["recursive", "sentence", "semantic", "by_section"] = "recursive"
    chunk_size: int = Field(default=512, ge=64, le=4096)
    overlap: int = Field(default=64, ge=0, le=512)
    min_chunk_size: int = Field(default=64, ge=32, le=256)
    separators: list[str] = Field(default_factory=lambda: ["\n\n", "\n", " "])
    document_type_overrides: dict[str, ChunkConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def overlap_less_than_chunk_size(self) -> ChunkConfig:
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        return self


class ValidationConfig(BaseModel):
    """Validated schema for collections.validation_config JSONB."""

    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    require_review: bool = False
    pii_action: Literal["block", "flag", "allow", "review"] = "flag"


class CollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    embedding_model_id: uuid.UUID
    chunk_config: ChunkConfig = Field(default_factory=ChunkConfig)
    validation_config: ValidationConfig = Field(default_factory=ValidationConfig)


class CollectionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    chunk_config: ChunkConfig | None = None
    validation_config: ValidationConfig | None = None
    is_active: bool | None = None


class EmbeddingModelRef(BaseModel):
    id: uuid.UUID
    name: str
    model_id: str
    provider: str
    params: dict[str, Any]

    model_config = {"from_attributes": True}


class CollectionResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None
    embedding_model: EmbeddingModelRef
    chunk_config: ChunkConfig
    validation_config: ValidationConfig
    is_active: bool
    document_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CollectionListResponse(BaseModel):
    items: list[CollectionResponse]
    total: int
    page: int
    page_size: int
