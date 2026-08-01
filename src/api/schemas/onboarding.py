"""Pydantic schemas for the tenant onboarding wizard API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Request schemas ───────────────────────────────────────────────────────────

class StartSessionRequest(BaseModel):
    tenant_name: str = Field(..., min_length=2, max_length=255)
    tenant_slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    contact_email: str = Field(..., max_length=255)


class Step1TenantConfigRequest(BaseModel):
    display_name: str = Field(..., min_length=2, max_length=255)
    settings: dict[str, Any] = Field(default_factory=dict)


class CollectionConfigItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    primary_language: str = Field(default="pol", max_length=10)
    chunk_strategy: str = Field(default="recursive", max_length=50)
    chunk_size: int = Field(default=512, ge=64, le=4096)
    chunk_overlap: int = Field(default=64, ge=0, le=512)


class Step2CollectionsRequest(BaseModel):
    collections: list[CollectionConfigItem] = Field(..., min_length=1, max_length=20)


class Step3AdminUserRequest(BaseModel):
    keycloak_user_id: str = Field(..., min_length=1, max_length=255)
    display_name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., max_length=255)
    role: str = Field(default="admin", max_length=50)


class Step4PipelineRequest(BaseModel):
    pipeline_name: str = Field(..., min_length=1, max_length=255)
    llm_model: str = Field(..., min_length=1, max_length=255)
    embedding_model: str = Field(..., min_length=1, max_length=255)
    top_k: int = Field(default=8, ge=1, le=50)
    score_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    cache_responses: bool = False
    guardrails_enabled: bool = True


# ── Response schemas ──────────────────────────────────────────────────────────

class SessionStatusResponse(BaseModel):
    session_id: uuid.UUID
    tenant_id: uuid.UUID | None
    status: str
    current_step: int
    steps_total: int = 5
    steps_completed: dict[str, bool]
    expires_at: datetime
    next_step_url: str | None = None


class StepResultResponse(BaseModel):
    session_id: uuid.UUID
    step_completed: int
    next_step: int | None
    message: str


class ActivationResultResponse(BaseModel):
    tenant_id: uuid.UUID
    tenant_slug: str
    collections_created: int
    pipeline_created: bool
    admin_user_assigned: bool
    activated_at: datetime
