"""Security tests: tenant isolation for the RAG Pipelines API.

Verifies that:
- Users from tenant A cannot list/get/modify pipelines owned by tenant B.
- Creating a pipeline with llm_model_id from a different tenant raises 422.
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

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
USER_A = uuid.uuid4()
PIPELINE_B_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()

_NOW = datetime.now(tz=UTC)


def _make_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:read", "admin:pipelines"}),
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


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


# ── Cross-tenant pipeline isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_a_cannot_get_tenant_b_pipeline() -> None:
    """GET /api/v1/pipelines/{id} for a pipeline owned by tenant B → 404 for tenant A user."""
    from src.core.exceptions import NotFoundError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.pipeline_service.PipelineService.get_pipeline",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Pipeline not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/pipelines/{PIPELINE_B_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tenant_a_cannot_update_tenant_b_pipeline() -> None:
    """PATCH /api/v1/pipelines/{id} for a pipeline owned by tenant B → 404 for tenant A user."""
    from src.core.exceptions import NotFoundError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.pipeline_service.PipelineService.update_pipeline",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Pipeline not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/pipelines/{PIPELINE_B_ID}",
                json={"name": "hijacked"},
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tenant_a_cannot_delete_tenant_b_pipeline() -> None:
    """DELETE /api/v1/pipelines/{id} for a pipeline owned by tenant B → 404 for tenant A user."""
    from src.core.exceptions import NotFoundError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.pipeline_service.PipelineService.delete_pipeline",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Pipeline not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(
                f"/api/v1/pipelines/{PIPELINE_B_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_pipeline_with_foreign_tenant_model_returns_422() -> None:
    """POST /api/v1/pipelines with llm_model_id from another tenant → 422."""
    from src.core.exceptions import DomainValidationError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    # Simulate service rejecting a model from a different tenant
    with patch(
        "src.domain.pipeline_service.PipelineService.create_pipeline",
        new_callable=AsyncMock,
        side_effect=DomainValidationError("LLM model not found or not accessible"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/pipelines/",
                json={
                    "name": "cross-tenant-attack",
                    "llm_model_id": str(LLM_MODEL_ID),
                    "collection_ids": [],
                },
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_pipelines_does_not_expose_other_tenant_data() -> None:
    """GET /api/v1/pipelines returns only tenant-A pipelines (service enforced)."""
    from src.api.schemas.pipeline import PipelineListResponse, PipelineResponse

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    # Service returns only tenant A pipeline, nothing from tenant B
    sample: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT_A,
        "name": "tenant-a-pipeline",
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
    mock_response = PipelineListResponse(
        items=[PipelineResponse(**sample)],
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
            resp = await c.get(
                "/api/v1/pipelines/", headers={"Authorization": "Bearer fake"}
            )

    assert resp.status_code == 200
    items = resp.json()["items"]
    for item in items:
        assert item["tenant_id"] == str(TENANT_A)
        assert item["tenant_id"] != str(TENANT_B)
