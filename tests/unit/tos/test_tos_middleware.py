"""Unit tests for TosCheckMiddleware.

Uses httpx AsyncClient with ASGITransport so that the full middleware stack is exercised
without running a real server.  Redis and TosRepository are mocked — the real dispatch()
implementation is exercised without modification.
"""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from src.api.middleware.tos_check import TosCheckMiddleware

TENANT_ID = uuid.uuid4()


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_jwt(tenant_id: uuid.UUID) -> str:
    """Build a fake (unsigned) JWT with a tenant_id claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_bytes = json.dumps({"sub": "user-1", "tenant_id": str(tenant_id)}).encode()
    payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesignature"


def _make_redis(*, cached: bytes | None) -> MagicMock:
    r = MagicMock()
    r.get = AsyncMock(return_value=cached)
    r.set = AsyncMock()
    return r


@asynccontextmanager
async def _mock_session_factory() -> Any:  # type: ignore[return]
    yield MagicMock()


def _build_app(redis: MagicMock) -> FastAPI:
    """Build a minimal FastAPI app with the real TosCheckMiddleware attached."""
    app = FastAPI()

    @app.get("/api/v1/some-resource")
    async def _handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @app.get("/api/v1/terms")
    async def _terms(request: Request) -> PlainTextResponse:
        return PlainTextResponse("terms")

    @app.post("/api/v1/tenants/{tenant_id}/terms/accept")
    async def _accept(tenant_id: uuid.UUID, request: Request) -> PlainTextResponse:
        return PlainTextResponse("accepted")

    @app.get("/api/v1/tenants/{tenant_id}/terms/status")
    async def _status(tenant_id: uuid.UUID, request: Request) -> PlainTextResponse:
        return PlainTextResponse("status")

    app.add_middleware(
        TosCheckMiddleware,
        redis=redis,
        session_factory=_mock_session_factory,
    )
    return app


# ── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_excluded_path_terms_passes_through() -> None:
    """GET /api/v1/terms is excluded — no ToS check performed."""
    redis = _make_redis(cached=b"0")  # Would block any non-excluded path
    app = _build_app(redis)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/terms")

    assert resp.status_code == 200
    assert resp.text == "terms"
    # Redis must NOT have been queried for excluded path
    redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_returns_403_when_redis_says_not_accepted() -> None:
    """Redis cached '0' (not accepted) → 403 with tos_required=True."""
    redis = _make_redis(cached=b"0")
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/some-resource",
            headers={"Authorization": f"Bearer {jwt}"},
        )

    assert resp.status_code == 403
    body = resp.json()
    assert body["tos_required"] is True


@pytest.mark.asyncio
async def test_passes_through_when_redis_says_accepted() -> None:
    """Redis cached '1' (accepted) → request passes through normally."""
    redis = _make_redis(cached=b"1")
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/some-resource",
            headers={"Authorization": f"Bearer {jwt}"},
        )

    assert resp.status_code == 200
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_no_jwt_passes_through_to_auth_layer() -> None:
    """Requests without Authorization header are forwarded (auth layer handles 401)."""
    redis = _make_redis(cached=None)
    app = _build_app(redis)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/some-resource")

    # No JWT → middleware skips ToS check; endpoint responds 200 (no auth in test app)
    assert resp.status_code == 200
    redis.get.assert_not_called()


@pytest.mark.asyncio
async def test_403_response_contains_correct_accept_url() -> None:
    """The 403 body must include the correct accept_url pointing to the tenant's endpoint."""
    redis = _make_redis(cached=b"0")
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/some-resource",
            headers={"Authorization": f"Bearer {jwt}"},
        )

    assert resp.status_code == 403
    body = resp.json()
    expected_url = f"/api/v1/tenants/{TENANT_ID}/terms/accept"
    assert body["accept_url"] == expected_url


@pytest.mark.asyncio
async def test_cache_miss_queries_db_and_blocks_when_not_accepted() -> None:
    """Cache miss (Redis returns None) → DB query → 403 when not accepted."""
    redis = _make_redis(cached=None)  # Cache miss
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)

    with patch("src.api.middleware.tos_check.TosRepository") as mock_repo:
        mock_repo_instance = MagicMock()
        mock_repo_instance.has_accepted_current_tos = AsyncMock(return_value=False)
        mock_repo.return_value = mock_repo_instance

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/some-resource",
                headers={"Authorization": f"Bearer {jwt}"},
            )

    assert resp.status_code == 403
    assert resp.json()["tos_required"] is True
    # Cache should be populated with "0"
    redis.set.assert_called_once_with(f"tos:tenant:{TENANT_ID}:accepted", "0", ex=60)


@pytest.mark.asyncio
async def test_cache_miss_queries_db_and_allows_when_accepted() -> None:
    """Cache miss → DB query → 200 when accepted; cache populated with '1'."""
    redis = _make_redis(cached=None)  # Cache miss
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)

    with patch("src.api.middleware.tos_check.TosRepository") as mock_repo:
        mock_repo_instance = MagicMock()
        mock_repo_instance.has_accepted_current_tos = AsyncMock(return_value=True)
        mock_repo.return_value = mock_repo_instance

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/some-resource",
                headers={"Authorization": f"Bearer {jwt}"},
            )

    assert resp.status_code == 200
    redis.set.assert_called_once_with(f"tos:tenant:{TENANT_ID}:accepted", "1", ex=60)


@pytest.mark.asyncio
async def test_terms_accept_endpoint_passes_through() -> None:
    """POST /api/v1/tenants/{id}/terms/accept bypasses ToS check."""
    redis = _make_redis(cached=b"0")  # Would block non-excluded paths
    app = _build_app(redis)

    jwt = _make_jwt(TENANT_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/api/v1/tenants/{TENANT_ID}/terms/accept",
            headers={"Authorization": f"Bearer {jwt}"},
        )

    assert resp.status_code == 200
    redis.get.assert_not_called()
