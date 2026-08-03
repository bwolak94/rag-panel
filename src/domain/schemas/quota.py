"""Pydantic schemas for quota management (TASK-026)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class QuotaItem(BaseModel):
    limit: int
    current: int
    pct: float
    reset_at: datetime | None = None


class QuotaStatusResponse(BaseModel):
    tenant_id: uuid.UUID
    quotas: dict[str, QuotaItem]


class UpdateQuotasRequest(BaseModel):
    max_documents: int | None = Field(default=None, ge=1)
    max_storage_bytes: int | None = Field(default=None, ge=1)
    max_monthly_queries: int | None = Field(default=None, ge=1)
    max_mau: int | None = Field(default=None, ge=1)
    max_collections: int | None = Field(default=None, ge=1)
