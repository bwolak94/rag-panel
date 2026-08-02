"""Per-tenant usage analytics router.

GET  /api/v1/admin/analytics/summary    → AnalyticsSummaryResponse
GET  /api/v1/admin/analytics/queries    → QuerySeriesResponse
GET  /api/v1/admin/analytics/documents  → DocumentStatsResponse
GET  /api/v1/admin/analytics/users      → UserStatsResponse

All endpoints require the 'admin:analytics' permission.
tenant_id is ALWAYS from JWT context — never from body/query/path.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.analytics import (
    AnalyticsSummaryResponse,
    DocumentStatsResponse,
    QuerySeriesResponse,
    UserStatsResponse,
)
from src.core.clients.redis_client import get_redis_client
from src.core.database import get_db_session
from src.domain.analytics_service import AnalyticsService
from src.domain.auth import UserContext

router = APIRouter(prefix="/api/v1/admin/analytics", tags=["admin-analytics"])

_RequireAnalytics = Annotated[None, Depends(require_permission("admin:analytics"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


def _get_service(session: AsyncSession) -> AnalyticsService:
    return AnalyticsService(session, get_redis_client())


@router.get(
    "/summary",
    response_model=AnalyticsSummaryResponse,
    summary="High-level usage summary for the tenant (period 1–90 days)",
)
async def get_analytics_summary(
    ctx: _Ctx,
    _: _RequireAnalytics,
    session: _Session,
    period_days: int = Query(default=30, ge=1, le=90),
) -> AnalyticsSummaryResponse:
    """Return active users, total queries, document counts and top collections."""
    return await _get_service(session).get_summary(ctx.tenant_id, period_days)


@router.get(
    "/queries",
    response_model=QuerySeriesResponse,
    summary="Time-series query counts grouped by day or week",
)
async def get_query_analytics(
    ctx: _Ctx,
    _: _RequireAnalytics,
    session: _Session,
    period: Literal["7d", "30d", "90d"] = Query(default="7d"),
    granularity: Literal["hour", "day", "week"] = Query(default="day"),
) -> QuerySeriesResponse:
    """Return query count, unique users and error count per time bucket."""
    return await _get_service(session).get_query_series(ctx.tenant_id, period, granularity)


@router.get(
    "/documents",
    response_model=DocumentStatsResponse,
    summary="Document counts by status and collection, with ingest success rate",
)
async def get_document_analytics(
    ctx: _Ctx,
    _: _RequireAnalytics,
    session: _Session,
) -> DocumentStatsResponse:
    """Return document breakdown by status and per-collection stats."""
    return await _get_service(session).get_document_stats(ctx.tenant_id)


@router.get(
    "/users",
    response_model=UserStatsResponse,
    summary="User activity: MAU, daily active users, top users by query count",
)
async def get_user_analytics(
    ctx: _Ctx,
    _: _RequireAnalytics,
    session: _Session,
    period_days: int = Query(default=30, ge=7, le=90),
) -> UserStatsResponse:
    """Return MAU, 7-day DAU series and top-10 users by query volume."""
    return await _get_service(session).get_user_stats(ctx.tenant_id, period_days)
