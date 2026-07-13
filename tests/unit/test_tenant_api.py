"""Unit tests for Tenant Management API endpoints.

get_current_ctx and get_db_session are overridden via dependency_overrides.
TenantService is patched at the router level — no DB, MinIO, or Keycloak needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.api.schemas.tenant import TenantMemberResponse, TenantResponse, TenantSettings
from src.core.database import get_db_session
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
TARGET_USER_ID = uuid.uuid4()

NOW = datetime.now(UTC)


def _make_ctx(permissions: frozenset[str], tenant_id: uuid.UUID = TENANT_ID) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-admin",
        email="admin@example.com",
        display_name="Admin",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=permissions,
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _platform_admin() -> UserContext:
    return _make_ctx(frozenset({"admin:tenants", "admin:users", "documents:read"}))


def _tenant_admin() -> UserContext:
    return _make_ctx(frozenset({"admin:users", "documents:read"}))


def _viewer() -> UserContext:
    return _make_ctx(frozenset({"documents:read"}))


def _make_tenant_response(**overrides: Any) -> TenantResponse:
    defaults: dict[str, Any] = {
        "id": TENANT_ID,
        "name": "Acme Clinic",
        "slug": "acme-clinic",
        "status": "active",
        "settings": TenantSettings(),
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return TenantResponse(**defaults)


def _make_member_response(**overrides: Any) -> TenantMemberResponse:
    defaults: dict[str, Any] = {
        "user_id": TARGET_USER_ID,
        "keycloak_sub": "kc-target",
        "email": "target@example.com",
        "display_name": "Target User",
        "role": "viewer",
        "joined_at": NOW,
    }
    defaults.update(overrides)
    return TenantMemberResponse(**defaults)


def _make_app(ctx: UserContext) -> Any:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        yield MagicMock(spec=AsyncSession)

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    return app


# ── Tenant CRUD tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_tenant_valid_returns_201() -> None:
    """POST /tenants with valid body as platform-admin → 201."""
    app = _make_app(_platform_admin())
    tenant_resp = _make_tenant_response()

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.create_tenant.return_value = tenant_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/tenants",
                json={"name": "Acme Clinic", "slug": "acme-clinic"},
            )

    assert resp.status_code == 201
    assert resp.json()["slug"] == "acme-clinic"


@pytest.mark.asyncio
async def test_create_tenant_as_viewer_returns_403() -> None:
    """POST /tenants without admin:tenants permission → 403."""
    app = _make_app(_viewer())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/tenants",
            json={"name": "X", "slug": "x-clinic"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_tenant_duplicate_slug_returns_409() -> None:
    """POST /tenants with slug that already exists → 409."""
    from src.core.exceptions import ConflictError

    app = _make_app(_platform_admin())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.create_tenant.side_effect = ConflictError("slug taken")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/api/v1/tenants",
                json={"name": "Acme", "slug": "acme-clinic"},
            )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_tenant_invalid_slug_returns_422() -> None:
    """POST /tenants with invalid slug (uppercase) → 422."""
    app = _make_app(_platform_admin())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/tenants",
            json={"name": "Acme", "slug": "ACME_CLINIC"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_tenant_slug_leading_dash_returns_422() -> None:
    """POST /tenants with slug starting with dash → 422."""
    app = _make_app(_platform_admin())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/tenants",
            json={"name": "X", "slug": "-bad"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_own_tenant_returns_200() -> None:
    """GET /tenants/{id} for own tenant → 200."""
    app = _make_app(_viewer())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.get_tenant.return_value = _make_tenant_response()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/tenants/{TENANT_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(TENANT_ID)


@pytest.mark.asyncio
async def test_get_different_tenant_returns_403() -> None:
    """GET /tenants/{other_id} where other_id != ctx.tenant_id → 403."""
    other_tenant = uuid.uuid4()
    app = _make_app(_viewer())  # ctx.tenant_id == TENANT_ID

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/tenants/{other_tenant}")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_patch_tenant_name_only_returns_200() -> None:
    """PATCH /tenants/{id} with only name → 200, other fields unchanged."""
    app = _make_app(_platform_admin())
    updated = _make_tenant_response(name="New Name")

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.update_tenant.return_value = updated

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(f"/api/v1/tenants/{TENANT_ID}", json={"name": "New Name"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"


@pytest.mark.asyncio
async def test_patch_tenant_invalid_settings_returns_422() -> None:
    """PATCH /tenants/{id} with retention_days < 30 → 422."""
    app = _make_app(_platform_admin())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.patch(
            f"/api/v1/tenants/{TENANT_ID}",
            json={"settings": {"retention_days": 5}},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_tenant_returns_204() -> None:
    """DELETE /tenants/{id} as platform-admin → 204."""
    app = _make_app(_platform_admin())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.delete_tenant.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/api/v1/tenants/{TENANT_ID}")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_tenant_as_viewer_returns_403() -> None:
    """DELETE /tenants/{id} as Viewer → 403."""
    app = _make_app(_viewer())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(f"/api/v1/tenants/{TENANT_ID}")

    assert resp.status_code == 403


# ── User membership tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_user_valid_returns_201() -> None:
    """POST /tenants/{id}/users with valid keycloak_sub → 201."""
    app = _make_app(_tenant_admin())
    member_resp = _make_member_response()

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.add_user.return_value = member_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/api/v1/tenants/{TENANT_ID}/users",
                json={"keycloak_sub": "kc-target"},
            )

    assert resp.status_code == 201
    assert resp.json()["user_id"] == str(TARGET_USER_ID)


@pytest.mark.asyncio
async def test_add_user_unknown_keycloak_sub_returns_404() -> None:
    """POST /tenants/{id}/users with unknown keycloak_sub → 404."""
    from src.core.exceptions import NotFoundError

    app = _make_app(_tenant_admin())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.add_user.side_effect = NotFoundError("user not found")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/api/v1/tenants/{TENANT_ID}/users",
                json={"keycloak_sub": "nobody"},
            )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_add_user_already_member_returns_409() -> None:
    """POST /tenants/{id}/users adding existing member → 409."""
    from src.core.exceptions import ConflictError

    app = _make_app(_tenant_admin())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.add_user.side_effect = ConflictError("already member")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/api/v1/tenants/{TENANT_ID}/users",
                json={"keycloak_sub": "kc-existing"},
            )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_remove_user_returns_204() -> None:
    """DELETE /tenants/{id}/users/{user_id} → 204."""
    app = _make_app(_tenant_admin())

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.remove_user.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/api/v1/tenants/{TENANT_ID}/users/{TARGET_USER_ID}")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_assign_role_valid_returns_200() -> None:
    """PUT /tenants/{id}/users/{user_id}/role with valid role → 200."""
    app = _make_app(_tenant_admin())
    member_resp = _make_member_response(role="contributor")

    with patch("src.api.routers.tenants._get_service") as mock_get_svc:
        mock_svc = AsyncMock()
        mock_get_svc.return_value = mock_svc
        mock_svc.assign_role.return_value = member_resp

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.put(
                f"/api/v1/tenants/{TENANT_ID}/users/{TARGET_USER_ID}/role",
                json={"role": "contributor"},
            )

    assert resp.status_code == 200
    assert resp.json()["role"] == "contributor"


@pytest.mark.asyncio
async def test_assign_role_invalid_role_returns_422() -> None:
    """PUT /tenants/{id}/users/{user_id}/role with invalid role → 422."""
    app = _make_app(_tenant_admin())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put(
            f"/api/v1/tenants/{TENANT_ID}/users/{TARGET_USER_ID}/role",
            json={"role": "superadmin"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_members_as_viewer_returns_403() -> None:
    """GET /tenants/{id}/users as Viewer → 403."""
    app = _make_app(_viewer())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/tenants/{TENANT_ID}/users")

    assert resp.status_code == 403
