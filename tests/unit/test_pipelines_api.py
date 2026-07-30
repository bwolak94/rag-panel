"""Unit tests for the RAG Pipelines API endpoints.

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
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()

_NOW = datetime.now(tz=UTC)

_SAMPLE_PIPELINE: dict[str, Any] = {
    "id": PIPELINE_ID,
    "tenant_id": TENANT_ID,
    "name": "my-pipeline",
    "collection_ids": [],
    "llm_model_id": LLM_MODEL_ID,
    "prompt_config": {
        "system_prompt_override": None,
        "temperature": 0.0,
        "max_tokens": 1024,
        "top_k_retrieval": 8,
        "score_threshold": 0.5,
    },
    "guardrails": {
        "block_topics": [],
        "max_response_tokens": 2048,
        "require_citations": True,
    },
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
    return _make_ctx(frozenset({"documents:read", "admin:pipelines"}))


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


# ── GET /api/v1/pipelines ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_pipelines_returns_200() -> None:
    """GET /api/v1/pipelines → 200 with paginated list."""
    from src.api.schemas.pipeline import PipelineListResponse, PipelineResponse

    app = _make_app(_viewer_ctx())
    mock_response = PipelineListResponse(
        items=[PipelineResponse(**_SAMPLE_PIPELINE)],
        total=1,
        page=1,
        page_size=20,
    )

    with patch(
        "src.domain.pipeline_service.PipelineService.list_pipelines",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/api/v1/pipelines/", headers={"Authorization": "Bearer fake"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["name"] == "my-pipeline"


@pytest.mark.asyncio
async def test_list_pipelines_requires_auth() -> None:
    """GET /api/v1/pipelines/ without Authorization → 403 (HTTPBearer auto_error=True)."""
    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/pipelines/")

    assert resp.status_code in (401, 403)


# ── POST /api/v1/pipelines ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_pipeline_returns_201() -> None:
    """POST /api/v1/pipelines → 201 with created pipeline."""
    from src.api.schemas.pipeline import PipelineResponse

    app = _make_app(_admin_ctx())
    mock_response = PipelineResponse(**_SAMPLE_PIPELINE)

    payload = {
        "name": "my-pipeline",
        "llm_model_id": str(LLM_MODEL_ID),
        "collection_ids": [],
    }

    with patch(
        "src.domain.pipeline_service.PipelineService.create_pipeline",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/pipelines/",
                json=payload,
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-pipeline"
    assert data["llm_model_id"] == str(LLM_MODEL_ID)


@pytest.mark.asyncio
async def test_create_pipeline_missing_required_fields_returns_422() -> None:
    """POST /api/v1/pipelines without required fields → 422."""
    app = _make_app(_admin_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/pipelines/",
            json={"name": "incomplete"},
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_pipeline_non_admin_returns_403() -> None:
    """POST /api/v1/pipelines by a user without admin:pipelines → 403."""
    app = _make_app(_viewer_ctx())

    payload = {
        "name": "my-pipeline",
        "llm_model_id": str(LLM_MODEL_ID),
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/pipelines/",
            json=payload,
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 403


# ── GET /api/v1/pipelines/{id} ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pipeline_returns_200() -> None:
    """GET /api/v1/pipelines/{id} for existing pipeline → 200."""
    from src.api.schemas.pipeline import PipelineResponse

    app = _make_app(_viewer_ctx())
    mock_response = PipelineResponse(**_SAMPLE_PIPELINE)

    with patch(
        "src.domain.pipeline_service.PipelineService.get_pipeline",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/pipelines/{PIPELINE_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(PIPELINE_ID)


@pytest.mark.asyncio
async def test_get_pipeline_not_found_returns_404() -> None:
    """GET /api/v1/pipelines/{id} for unknown pipeline → 404."""
    from src.core.exceptions import NotFoundError

    app = _make_app(_viewer_ctx())

    with patch(
        "src.domain.pipeline_service.PipelineService.get_pipeline",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Pipeline not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/pipelines/{uuid.uuid4()}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_pipeline_invalid_uuid_returns_422() -> None:
    """GET /api/v1/pipelines/not-a-uuid → 422."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/v1/pipelines/not-a-uuid",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 422


# ── PATCH /api/v1/pipelines/{id} ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_pipeline_returns_200() -> None:
    """PATCH /api/v1/pipelines/{id} → 200 with updated pipeline."""
    from src.api.schemas.pipeline import PipelineResponse

    app = _make_app(_admin_ctx())
    updated = {**_SAMPLE_PIPELINE, "name": "updated-pipeline"}
    mock_response = PipelineResponse(**updated)

    with patch(
        "src.domain.pipeline_service.PipelineService.update_pipeline",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/pipelines/{PIPELINE_ID}",
                json={"name": "updated-pipeline"},
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 200
    assert resp.json()["name"] == "updated-pipeline"


# ── DELETE /api/v1/pipelines/{id} ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_pipeline_returns_204() -> None:
    """DELETE /api/v1/pipelines/{id} → 204."""
    app = _make_app(_admin_ctx())

    with patch(
        "src.domain.pipeline_service.PipelineService.delete_pipeline",
        new_callable=AsyncMock,
        return_value=None,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(
                f"/api/v1/pipelines/{PIPELINE_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_pipeline_non_admin_returns_403() -> None:
    """DELETE /api/v1/pipelines/{id} by user without admin:pipelines → 403."""
    app = _make_app(_viewer_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(
            f"/api/v1/pipelines/{PIPELINE_ID}",
            headers={"Authorization": "Bearer fake"},
        )

    assert resp.status_code == 403
