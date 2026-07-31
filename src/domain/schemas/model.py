"""Pydantic schemas for the Models Registry API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelCreate(BaseModel):
    """Schema for creating a new model entry in the registry."""

    name: str = Field(..., min_length=1, max_length=255)
    type: Literal["llm", "embedding"]
    provider: Literal["ollama", "vllm"]
    endpoint_url: str = Field(..., max_length=500)
    model_id: str = Field(..., min_length=1, max_length=255)
    params: dict[str, Any] = Field(default_factory=dict)
    allowed_roles: list[uuid.UUID] = Field(default_factory=list)


class ModelUpdate(BaseModel):
    """Schema for partial update of a model entry."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    endpoint_url: str | None = Field(default=None, max_length=500)
    params: dict[str, Any] | None = None
    allowed_roles: list[uuid.UUID] | None = None
    is_active: bool | None = None


class ModelResponse(BaseModel):
    """Schema for model entry response."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None  # None = system-wide
    name: str
    type: str
    provider: str
    endpoint_url: str
    model_id: str
    params: dict[str, Any]
    allowed_roles: list[uuid.UUID]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ModelListResponse(BaseModel):
    """Paginated list of model entries."""

    items: list[ModelResponse]
    total: int
    page: int
    page_size: int


class ModelReachableResponse(BaseModel):
    """Response for the model reachability check endpoint."""

    model_id: uuid.UUID
    reachable: bool
    checked_at: str  # ISO-8601 timestamp
