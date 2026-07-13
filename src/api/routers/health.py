"""Health check endpoints.

/health       — combined liveness + dependency status (for load balancers)
/health/live  — liveness probe (Kubernetes livenessProbe)
/health/ready — readiness probe (Kubernetes readinessProbe); returns 503 if not ready
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from src.core.config import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    checks: dict[str, Literal["ok", "error"]]


async def _check_postgres() -> bool:
    try:
        from src.core.database import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(str(settings.REDIS_URL), socket_connect_timeout=2)
        try:
            await client.ping()
            return True
        finally:
            await client.close()
    except Exception:
        return False


async def _check_qdrant() -> bool:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{settings.QDRANT_URL}/healthz")
            return resp.status_code == 200
    except Exception:
        return False


async def _check_minio() -> bool:
    try:
        import httpx

        scheme = "https" if settings.MINIO_USE_TLS else "http"
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{scheme}://{settings.MINIO_ENDPOINT}/minio/health/live")
            return resp.status_code == 200
    except Exception:
        return False


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Combined health check. Always returns HTTP 200 to avoid restart loops."""
    checks = {
        "postgres": "ok" if await _check_postgres() else "error",
        "redis": "ok" if await _check_redis() else "error",
        "qdrant": "ok" if await _check_qdrant() else "error",
        "minio": "ok" if await _check_minio() else "error",
    }
    all_ok = all(v == "ok" for v in checks.values())
    return HealthResponse(
        status="ok" if all_ok else "degraded",
        version=settings.APP_VERSION,
        checks=checks,
    )


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is alive."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """Readiness probe — returns 503 if Postgres or Redis are unreachable."""
    pg_ok = await _check_postgres()
    redis_ok = await _check_redis()
    if pg_ok and redis_ok:
        return JSONResponse(status_code=200, content={"status": "ready"})
    return JSONResponse(
        status_code=503,
        content={
            "status": "not_ready",
            "checks": {
                "postgres": "ok" if pg_ok else "error",
                "redis": "ok" if redis_ok else "error",
            },
        },
    )
