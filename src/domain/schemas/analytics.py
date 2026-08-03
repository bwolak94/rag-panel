"""Pydantic v2 schemas for the per-tenant usage analytics API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class CollectionQueryStats(BaseModel):
    collection_id: uuid.UUID
    name: str
    query_count: int


class AnalyticsSummaryResponse(BaseModel):
    tenant_id: uuid.UUID
    period_days: int
    active_users: int
    total_queries: int
    total_documents: int
    documents_ready: int
    documents_failed: int
    documents_needs_review: int
    avg_query_latency_ms: float | None
    error_rate_pct: float
    top_collections: list[CollectionQueryStats]
    generated_at: datetime
    cached: bool


class QueryDataPoint(BaseModel):
    date: str  # ISO date string "YYYY-MM-DD"
    query_count: int
    unique_users: int
    error_count: int


class QuerySeriesResponse(BaseModel):
    series: list[QueryDataPoint]
    total_queries: int
    period: str
    granularity: str


class CollectionDocumentStats(BaseModel):
    collection_id: uuid.UUID
    name: str
    document_count: int
    total_size_bytes: int


class DocumentStatsResponse(BaseModel):
    by_status: dict[str, int]
    by_collection: list[CollectionDocumentStats]
    ingest_success_rate_pct: float
    avg_ingest_duration_ms: float | None


class DailyActiveUsers(BaseModel):
    date: str  # ISO date string "YYYY-MM-DD"
    active_users: int


class TopUser(BaseModel):
    user_id: uuid.UUID
    display_name: str
    query_count: int


class UserStatsResponse(BaseModel):
    mau_30d: int
    dau_7d: list[DailyActiveUsers]
    top_users_by_queries: list[TopUser]
