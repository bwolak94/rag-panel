"""OpenAI-compatible request and response schemas for the chat endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(..., min_length=1)
    stream: bool = False
    conversation_id: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class MessageSourceOut(BaseModel):
    document_id: str
    collection_id: str
    document_title: str
    section_heading: str | None = None
    source_url: str | None = None
    chunk_id: str | None = None
    page_number: int | None = None
    highlight_text: str | None = None
    relevance_score: float


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop", "length", "content_filter"] = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage
    message_sources: list[MessageSourceOut] = Field(default_factory=list)
    conversation_id: str | None = None
