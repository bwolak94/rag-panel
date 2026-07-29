"""Tenant isolation tests for ToS endpoints.

Verifies that cross-tenant access is correctly blocked.
Marked with @pytest.mark.tenant_isolation — treated as merge blockers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
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
USER_B = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
TOS_VERSION_ID = uuid.uuid4()


def _make_ctx(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID = USER_A,
    roles: frozenset[str] = frozenset({"owner"}),
) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=roles,
        permissions=frozenset({"chat:query"}),
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset({COLLECTION_ID}),
    )


def _make_app(ctx: UserContext) -> object:
    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock(spec=AsyncSession)
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        yield session

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    return app


# ── POST /tenants/{tenant_id}/terms/accept isolation ─────────────────────


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_accept_tos_cross_tenant_returns_403() -> None:
    """Owner of TENANT_A cannot accept ToS on behalf of TENANT_B.

    assert_tenant_owns_resource checks ctx.tenant_id == path param tenant_id.
    """
    ctx_a = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"owner"}))
    app = _make_app(ctx_a)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/api/v1/tenants/{TENANT_B}/terms/accept",
            json={"tos_version_id": str(TOS_VERSION_ID), "explicit_consent": True},
        )

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_accept_tos_non_owner_returns_403() -> None:
    """Tenant Admin (not Owner) cannot accept ToS — Owner role required."""
    ctx_admin = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"admin"}))
    app = _make_app(ctx_admin)

    with patch(
        "src.domain.tos_service.TosService.accept_tos",
        new=AsyncMock(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/api/v1/tenants/{TENANT_A}/terms/accept",
                json={"tos_version_id": str(TOS_VERSION_ID), "explicit_consent": True},
            )

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_accept_tos_contributor_returns_403() -> None:
    """Contributor role cannot accept ToS."""
    ctx_contributor = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"contributor"}))
    app = _make_app(ctx_contributor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/api/v1/tenants/{TENANT_A}/terms/accept",
            json={"tos_version_id": str(TOS_VERSION_ID), "explicit_consent": True},
        )

    assert resp.status_code == 403


# ── GET /tenants/{tenant_id}/terms/status isolation ──────────────────────


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_tos_status_cross_tenant_returns_403() -> None:
    """TENANT_A user cannot read TENANT_B's ToS status."""
    ctx_a = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"owner"}))
    app = _make_app(ctx_a)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/tenants/{TENANT_B}/terms/status")

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_tos_status_viewer_returns_403() -> None:
    """Viewer role cannot access ToS status — Owner or Admin required."""
    ctx_viewer = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"viewer"}))
    app = _make_app(ctx_viewer)

    with patch(
        "src.domain.tos_service.TosService.get_tos_status",
        new=AsyncMock(
            return_value={
                "current_tos_version": "1.0",
                "is_accepted": True,
                "accepted_at": None,
                "accepted_by": None,
                "requires_reacceptance": False,
                "ui_message": None,
            }
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/tenants/{TENANT_A}/terms/status")

    assert resp.status_code == 403


# ── explicit_consent validation ───────────────────────────────────────────


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_accept_tos_explicit_consent_false_returns_422() -> None:
    """explicit_consent=False must result in 422 (DomainValidationError)."""
    from src.core.exceptions import DomainValidationError

    ctx = _make_ctx(tenant_id=TENANT_A, roles=frozenset({"owner"}))
    app = _make_app(ctx)

    with patch(
        "src.domain.tos_service.TosService.accept_tos",
        new=AsyncMock(
            side_effect=DomainValidationError(
                "explicit_consent must be True to accept the Terms of Service"
            )
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                f"/api/v1/tenants/{TENANT_A}/terms/accept",
                json={"tos_version_id": str(TOS_VERSION_ID), "explicit_consent": False},
            )

    assert resp.status_code == 422
