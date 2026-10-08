"""Pydantic schemas for the RAG Pipelines API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ABTestConfig(BaseModel):
    """Configuration for shadow-mode prompt A/B testing."""

    enabled: bool = False
    shadow_prompt_version: str = Field(default="", min_length=0, max_length=32)
    traffic_split: float = Field(default=0.1, ge=0.0, le=1.0)
    experiment_id: str = Field(default="", min_length=0, max_length=128)

    @model_validator(mode="after")
    def validate_enabled_requires_fields(self) -> ABTestConfig:
        """Require non-empty shadow_prompt_version and experiment_id when enabled=True."""
        if self.enabled:
            if not self.shadow_prompt_version:
                raise ValueError("shadow_prompt_version must be set when enabled=True")
            if not self.experiment_id:
                raise ValueError("experiment_id must be set when enabled=True")
        return self


class PromptConfig(BaseModel):
    """Validated prompt configuration for a RAG pipeline."""

    system_prompt_override: str | None = Field(default=None, max_length=8000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=64, le=8192)
    top_k_retrieval: int = Field(default=8, ge=1, le=50)
    score_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_context_tokens: int = Field(
        default=3072,
        ge=256,
        le=16384,
        description="Maximum tokens allocated to chunk context in the generation prompt (ADR-021).",
    )
    ab_test: ABTestConfig | None = Field(default=None)


class GuardrailsConfig(BaseModel):
    """Validated guardrails configuration for a RAG pipeline.

    ADR-017: llm_check enables the structured LLM safety evaluation pass.
    add_disclaimer appends the medical disclaimer to substantive answers.
    """

    block_topics: list[str] = Field(default_factory=list)
    max_response_tokens: int = Field(default=2048, ge=64, le=8192)
    require_citations: bool = True
    add_disclaimer: bool = Field(
        default=False,
        description="Append the medical disclaimer to substantive answers.",
    )
    llm_check: bool = Field(
        default=False,
        description=(
            "ADR-017: Enable structured LLM safety evaluation (factual grounding, "
            "PII detection, prompt injection). Uses guardrails_output_v2 prompt. "
            "Fail-open with 30 s timeout; ~2-5 s latency when triggered."
        ),
    )


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
