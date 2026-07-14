"""Request and response schemas for conversation management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ConversationCreate(BaseModel):
    pipeline_id: uuid.UUID
    title: str | None = None


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    pipeline_id: uuid.UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID | None
    chunk_id: uuid.UUID | None
    relevance_score: float
    highlight_text: str | None
    page_number: int | None


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    model_id: uuid.UUID | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float | None
    created_at: datetime
    sources: list[MessageSourceResponse] = []


class ConversationDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    pipeline_id: uuid.UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]


class FeedbackCreate(BaseModel):
    rating: Literal["up", "down"]
    comment: str | None = None


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    rating: str
    comment: str | None
    created_at: datetime
