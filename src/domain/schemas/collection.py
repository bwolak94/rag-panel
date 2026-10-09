"""Pydantic schemas for the Collections API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ChunkStrategyConfig(BaseModel):
    """Per-document-type chunking strategy configuration (ADR-015).

    Used as values in ``ChunkConfig.type_overrides`` to override both strategy
    and size parameters for a specific document type (e.g., ``"table"``,
    ``"clinical_note"``).
    """

    strategy: Literal["recursive", "section_aware", "sentence", "row"] = "recursive"
    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=512)

    @model_validator(mode="after")
    def chunk_overlap_less_than_chunk_size(self) -> ChunkStrategyConfig:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self


class LegacyChunkSizeOverride(BaseModel):
    """Flat size-only override for backward-compat JSONB records.

    Pre-ADR-015 collections stored ``document_type_overrides`` entries that contained
    only ``chunk_size`` and ``overlap``.  This model replaces the old self-referential
    ``dict[str, ChunkConfig]`` type to prevent unbounded recursive nesting.
    """

    chunk_size: int = Field(default=512, ge=64, le=4096)
    overlap: int = Field(default=64, ge=0, le=512)

    @model_validator(mode="after")
    def overlap_less_than_chunk_size(self) -> LegacyChunkSizeOverride:
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap must be less than chunk_size")
        return self


class ChunkConfig(BaseModel):
    """Validated schema for collections.chunk_config JSONB."""

    strategy: Literal["recursive", "section_aware", "sentence", "row"] = "recursive"
    chunk_size: int = Field(default=512, ge=64, le=4096)
    overlap: int = Field(default=64, ge=0, le=512)
    min_chunk_size: int = Field(default=64, ge=32, le=256)
    separators: list[str] = Field(default_factory=lambda: ["\n\n", "\n", " "])
    # ADR-015: per-document-type full strategy override (strategy + size).
    # Keys are document_type values from ValidationResult (e.g. "table", "clinical_note").
    type_overrides: dict[str, ChunkStrategyConfig] = Field(default_factory=dict)
    # Backward-compat: flat size-only overrides from pre-ADR-015 collections.
    # Prefer type_overrides for new configurations.
    document_type_overrides: dict[str, LegacyChunkSizeOverride] = Field(default_factory=dict)

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
