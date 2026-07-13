"""Tests for health check endpoints."""

from unittest.mock import patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_all_ok(async_client: AsyncClient) -> None:
    with (
        patch("src.api.routers.health._check_postgres", return_value=True),
        patch("src.api.routers.health._check_redis", return_value=True),
        patch("src.api.routers.health._check_qdrant", return_value=True),
        patch("src.api.routers.health._check_minio", return_value=True),
    ):
        resp = await async_client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert all(v == "ok" for v in data["checks"].values())


@pytest.mark.asyncio
async def test_health_degraded_when_postgres_down(async_client: AsyncClient) -> None:
    with (
        patch("src.api.routers.health._check_postgres", return_value=False),
        patch("src.api.routers.health._check_redis", return_value=True),
        patch("src.api.routers.health._check_qdrant", return_value=True),
        patch("src.api.routers.health._check_minio", return_value=True),
    ):
        resp = await async_client.get("/health")
    assert resp.status_code == 200  # still 200 — degraded, not down
    assert resp.json()["status"] == "degraded"
    assert resp.json()["checks"]["postgres"] == "error"


@pytest.mark.asyncio
async def test_liveness_always_200(async_client: AsyncClient) -> None:
    resp = await async_client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_readiness_503_when_postgres_down(async_client: AsyncClient) -> None:
    with (
        patch("src.api.routers.health._check_postgres", return_value=False),
        patch("src.api.routers.health._check_redis", return_value=True),
    ):
        resp = await async_client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["postgres"] == "error"


@pytest.mark.asyncio
async def test_readiness_200_when_all_ready(async_client: AsyncClient) -> None:
    with (
        patch("src.api.routers.health._check_postgres", return_value=True),
        patch("src.api.routers.health._check_redis", return_value=True),
    ):
        resp = await async_client.get("/health/ready")
    assert resp.status_code == 200
