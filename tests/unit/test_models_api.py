"""Unit tests for the Models Registry API endpoints.

All service-layer calls are mocked — no DB required.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.core.database import get_db_session
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()

_NOW = datetime.now(tz=UTC)

_SAMPLE_MODEL: dict[str, Any] = {
    "id": MODEL_ID,
    "tenant_id": TENANT_ID,
    "name": "my-llm",
    "type": "llm",
    "provider": "ollama",
    "endpoint_url": "http://ollama:11434",
    "model_id": "mistral:7b",
    "params": {},
    "allowed_roles": [],
    "is_active": True,
    "created_at": _NOW,
    "updated_at": _NOW,
}


def _make_ctx(permissions: frozenset[str]) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=permissions,
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _admin_ctx() -> UserContext:
    return _make_ctx(frozenset({"documents:read", "admin:models"}))


def _viewer_ctx() -> UserContext:
    return _make_ctx(frozenset({"documents:read"}))


def _make_app(ctx: UserContext) -> Any:
    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock(spec=AsyncSession)
        yield session

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    return app


# ── GET /api/v1/models ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models_returns_200() -> None:
    """GET /api/v1/models → 200 with paginated list."""
    from src.api.schemas.model import ModelListResponse, ModelResponse

    app = _make_app(_viewer_ctx())
    mock_response = ModelListResponse(
        items=[ModelResponse(**_SAMPLE_MODEL)],
        total=1,
        page=1,
        page_size=20,
    )

    with patch(
        "src.domain.model_service.ModelService.list_models",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=True
        ) as c:
            resp = await c.get(
                "/api/v1/models/", headers={"Authorization": "Bearer fake"}
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "my-llm"


@pytest.mark.asyncio
async def test_list_models_requires_auth() -> None:
    """GET /api/v1/models/ without Authorization → 403 (HTTPBearer auto_error=True)."""
    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/models/")

    assert resp.status_code in (401, 403)


# ── POST /api/v1/models ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_model_returns_201() -> None:
    """POST /api/v1/models → 201 with created model."""
    from src.api.schemas.model import ModelResponse

    app = _make_app(_admin_ctx())
    mock_response = ModelResponse(**_SAMPLE_MODEL)

    payload = {
        "name": "my-llm",
        "type": "llm",
        "provider": "ollama",
        "endpoint_url": "http://ollama:11434",
        "model_id": "mistral:7b",
    }

    with patch(
        "src.domain.model_service.ModelService.create_model",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/models/",
                json=payload,
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-llm"
    assert data["type"] == "llm"


@pytest.mark.asyncio
async def test_create_model_missing_required_fields_returns_422() -> None:
    """POST /api/v1/models without required fields → 422."""
    app = _make_app(_admin_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/models/",
            json={"name": "incomplete"},
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_model_non_admin_returns_403() -> None:
    """POST /api/v1/models by a user without admin:models → 403."""
    app = _make_app(_viewer_ctx())

    payload = {
        "name": "my-llm",
        "type": "llm",
        "provider": "ollama",
        "endpoint_url": "http://ollama:11434",
        "model_id": "mistral:7b",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/models/",
            json=payload,
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 403


# ── GET /api/v1/models/{id} ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_model_returns_200() -> None:
    """GET /api/v1/models/{id} for existing model → 200."""
    from src.api.schemas.model import ModelResponse

    app = _make_app(_viewer_ctx())
    mock_response = ModelResponse(**_SAMPLE_MODEL)

    with patch(
        "src.domain.model_service.ModelService.get_model",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/models/{MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(MODEL_ID)


@pytest.mark.asyncio
async def test_get_model_not_found_returns_404() -> None:
    """GET /api/v1/models/{id} for unknown model → 404."""
    from src.core.exceptions import NotFoundError

    app = _make_app(_viewer_ctx())

    with patch(
        "src.domain.model_service.ModelService.get_model",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Model not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/models/{uuid.uuid4()}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_model_invalid_uuid_returns_422() -> None:
    """GET /api/v1/models/not-a-uuid → 422."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/models/not-a-uuid",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 422


# ── PATCH /api/v1/models/{id} ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_model_returns_200() -> None:
    """PATCH /api/v1/models/{id} → 200 with updated model."""
    from src.api.schemas.model import ModelResponse

    app = _make_app(_admin_ctx())
    updated = {**_SAMPLE_MODEL, "name": "updated-name"}
    mock_response = ModelResponse(**updated)

    with patch(
        "src.domain.model_service.ModelService.update_model",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/models/{MODEL_ID}",
                json={"name": "updated-name"},
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 200
    assert resp.json()["name"] == "updated-name"


# ── DELETE /api/v1/models/{id} ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivate_model_returns_204() -> None:
    """DELETE /api/v1/models/{id} → 204."""
    app = _make_app(_admin_ctx())

    with patch(
        "src.domain.model_service.ModelService.deactivate_model",
        new_callable=AsyncMock,
        return_value=None,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(
                f"/api/v1/models/{MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_deactivate_model_with_active_pipelines_returns_409() -> None:
    """DELETE /api/v1/models/{id} when model has active pipelines → 409."""
    from src.core.exceptions import ConflictError

    app = _make_app(_admin_ctx())

    with patch(
        "src.domain.model_service.ModelService.deactivate_model",
        new_callable=AsyncMock,
        side_effect=ConflictError("1 active pipeline(s) reference it"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(
                f"/api/v1/models/{MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_model_non_admin_returns_403() -> None:
    """DELETE /api/v1/models/{id} by user without admin:models → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(
            f"/api/v1/models/{MODEL_ID}",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 403
