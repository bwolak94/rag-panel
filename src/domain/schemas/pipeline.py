"""Pydantic schemas for the RAG Pipelines API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PromptConfig(BaseModel):
    """Validated prompt configuration for a RAG pipeline."""

    system_prompt_override: str | None = Field(default=None, max_length=8000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64, le=8192)
    top_k_retrieval: int = Field(default=8, ge=1, le=50)
    score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class GuardrailsConfig(BaseModel):
    """Validated guardrails configuration for a RAG pipeline."""

    block_topics: list[str] = Field(default_factory=list)
    max_response_tokens: int = Field(default=2048, ge=64, le=8192)
    require_citations: bool = True


class PipelineCreate(BaseModel):
    """Schema for creating a new RAG pipeline."""

    name: str = Field(..., min_length=1, max_length=255)
    collection_ids: list[uuid.UUID] = Field(default_factory=list)
    llm_model_id: uuid.UUID
    prompt_config: PromptConfig = Field(default_factory=PromptConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)


class PipelineUpdate(BaseModel):
    """Schema for partial update of a RAG pipeline."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    collection_ids: list[uuid.UUID] | None = None
    llm_model_id: uuid.UUID | None = None
    prompt_config: PromptConfig | None = None
    guardrails: GuardrailsConfig | None = None
    is_active: bool | None = None


class PipelineResponse(BaseModel):
    """Schema for RAG pipeline response."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    collection_ids: list[uuid.UUID]
    llm_model_id: uuid.UUID
    prompt_config: dict[str, Any]
    guardrails: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PipelineListResponse(BaseModel):
    """Paginated list of RAG pipelines."""

    items: list[PipelineResponse]
    total: int
    page: int
    page_size: int
