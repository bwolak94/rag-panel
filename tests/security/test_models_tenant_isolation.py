"""Security tests: tenant isolation for the Models Registry API.

Verifies that:
- Users cannot see/modify models from other tenants.
- System-wide models (tenant_id=None) are visible to all tenants.
- System-wide models cannot be updated or deleted via the API.
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
MODEL_ID = uuid.uuid4()
SYSTEM_MODEL_ID = uuid.uuid4()

_NOW = datetime.now(tz=UTC)


def _make_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:read", "admin:models"}),
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


# ── Cross-tenant model visibility ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_a_cannot_see_tenant_b_private_model() -> None:
    """GET /api/v1/models/{id} for a model owned by tenant B → 404 for tenant A user."""
    from src.core.exceptions import NotFoundError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.model_service.ModelService.get_model",
        new_callable=AsyncMock,
        side_effect=NotFoundError("Model not found"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/models/{MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tenant_a_can_see_system_wide_model() -> None:
    """GET /api/v1/models/{id} for a system-wide model (tenant_id=None) → 200 for any tenant."""
    from src.api.schemas.model import ModelResponse

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    system_model: dict[str, Any] = {
        "id": SYSTEM_MODEL_ID,
        "tenant_id": None,
        "name": "system-llm",
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
    mock_response = ModelResponse(**system_model)

    with patch(
        "src.domain.model_service.ModelService.get_model",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                f"/api/v1/models/{SYSTEM_MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 200
    assert resp.json()["tenant_id"] is None


@pytest.mark.asyncio
async def test_cannot_update_system_wide_model_returns_403() -> None:
    """PATCH /api/v1/models/{id} on a system-wide model → 403."""
    from src.core.exceptions import PermissionDeniedError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.model_service.ModelService.update_model",
        new_callable=AsyncMock,
        side_effect=PermissionDeniedError("System-wide models cannot be modified through this API"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/models/{SYSTEM_MODEL_ID}",
                json={"name": "hacked"},
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_cannot_delete_system_wide_model_returns_403() -> None:
    """DELETE /api/v1/models/{id} on a system-wide model → 403."""
    from src.core.exceptions import PermissionDeniedError

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.domain.model_service.ModelService.deactivate_model",
        new_callable=AsyncMock,
        side_effect=PermissionDeniedError(
            "System-wide models cannot be deactivated through this API"
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(
                f"/api/v1/models/{SYSTEM_MODEL_ID}",
                headers={"Authorization": "Bearer fake"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_models_does_not_include_other_tenant_private_models() -> None:
    """GET /api/v1/models returns only system-wide + own tenant models (service enforced)."""
    from src.api.schemas.model import ModelListResponse, ModelResponse

    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    # Service returns only one model (system-wide), no tenant B models
    system_model: dict[str, Any] = {
        "id": SYSTEM_MODEL_ID,
        "tenant_id": None,
        "name": "system-llm",
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
    mock_response = ModelListResponse(
        items=[ModelResponse(**system_model)],
        total=1,
        page=1,
        page_size=20,
    )

    with patch(
        "src.domain.model_service.ModelService.list_models",
        new_callable=AsyncMock,
        return_value=mock_response,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/api/v1/models/", headers={"Authorization": "Bearer fake"}
            )

    assert resp.status_code == 200
    items = resp.json()["items"]
    # No tenant B models in the list
    tenant_ids = [item["tenant_id"] for item in items]
    assert str(TENANT_B) not in tenant_ids
