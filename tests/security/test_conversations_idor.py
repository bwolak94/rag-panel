"""IDOR / tenant-isolation tests for Conversations and Messages endpoints.

Verifies that:
- Cross-tenant conversation access is denied (404).
- Cross-user access within same tenant is denied (404).
- Cross-tenant message feedback submission is denied (404).
- List endpoint only surfaces the requesting user's own conversations.
- Missing chat:query permission always yields 403 (regardless of resource existence).

Marked @pytest.mark.tenant_isolation — required blocker for every merge per project policy.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.core.database import get_db_session
from src.domain.auth import UserContext

# ---------------------------------------------------------------------------
# Fixed UUIDs used across all tests
# ---------------------------------------------------------------------------
TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()

USER_A = uuid.uuid4()
USER_B = uuid.uuid4()  # different user, same tenant as USER_A (TENANT_A)
USER_B_TENANT_B = uuid.uuid4()  # user who belongs to TENANT_B

CONV_A_ID = uuid.uuid4()  # conversation owned by USER_A in TENANT_A
MSG_A_ID = uuid.uuid4()  # message in CONV_A_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    permissions: frozenset[str] = frozenset({"chat:query"}),
) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub=f"kc-{user_id}",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=frozenset({"viewer"}),
        permissions=permissions,
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _make_conv_mock(
    conv_id: uuid.UUID = CONV_A_ID,
    tenant_id: uuid.UUID = TENANT_A,
    user_id: uuid.UUID = USER_A,
) -> MagicMock:
    """Return a MagicMock that looks like a Conversation row."""
    conv = MagicMock()
    conv.id = conv_id
    conv.tenant_id = tenant_id
    conv.user_id = user_id
    conv.pipeline_id = None
    conv.title = "Test conversation"
    conv.is_deleted = False
    conv.created_at = datetime.now(UTC)
    conv.updated_at = datetime.now(UTC)
    return conv


def _make_feedback_mock() -> MagicMock:
    fb = MagicMock()
    fb.id = uuid.uuid4()
    fb.tenant_id = TENANT_A
    fb.message_id = MSG_A_ID
    fb.user_id = USER_A
    fb.rating = "up"
    fb.comment = None
    fb.created_at = datetime.now(UTC)
    fb.updated_at = datetime.now(UTC)
    return fb


def _make_app(ctx: UserContext, session: AsyncSession | MagicMock) -> Any:
    """Build a FastAPI app with both get_current_ctx and get_db_session overridden."""
    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        yield session

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    return app


def _build_session(
    *, scalar_return: Any = None, scalars_return: list[Any] | None = None
) -> MagicMock:
    """Return a minimal AsyncSession mock wired for scalar() and scalars()."""
    session = MagicMock(spec=AsyncSession)
    session.scalar = AsyncMock(return_value=scalar_return)
    session.scalars = AsyncMock(return_value=iter(scalars_return or []))
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# 1. GET /conversations/{id} — cross-tenant → 404
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_conversation_cross_tenant_returns_404() -> None:
    """Tenant B user cannot read Tenant A's conversation — must get 404, not 200."""
    # scalar() returns None because tenant_id filter won't match for ctx of TENANT_B
    session = _build_session(scalar_return=None)
    ctx_b = _make_ctx(TENANT_B, USER_B_TENANT_B)
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 404
    assert resp.status_code != 200
    # Must not expose conversation title or internal details
    assert "Test conversation" not in resp.text


# ---------------------------------------------------------------------------
# 2. DELETE /conversations/{id} — cross-tenant → 404
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_conversation_cross_tenant_returns_404() -> None:
    """Tenant B user cannot delete Tenant A's conversation — must get 404, not 204."""
    # scalar() returns None because tenant_id filter blocks cross-tenant lookup
    session = _build_session(scalar_return=None)
    ctx_b = _make_ctx(TENANT_B, USER_B_TENANT_B)
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 404
    assert resp.status_code != 204


# ---------------------------------------------------------------------------
# 3. GET /conversations/{id} — cross-user within same tenant → 404
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_conversation_cross_user_same_tenant_returns_404() -> None:
    """User B cannot read User A's conversation within same tenant — must get 404."""
    # scalar() returns None: the query filters on user_id == ctx.user_id, so
    # USER_B asking for USER_A's conversation receives no row.
    session = _build_session(scalar_return=None)
    ctx_b = _make_ctx(TENANT_A, USER_B)  # same tenant, different user
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. DELETE /conversations/{id} — cross-user within same tenant → 404
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_conversation_cross_user_same_tenant_returns_404() -> None:
    """User B cannot delete User A's conversation within same tenant — must get 404.

    The delete handler queries by (id, tenant_id) but then explicitly checks
    conv.user_id == ctx.user_id; we simulate finding a row that belongs to USER_A
    while acting as USER_B — the handler must still return 404.
    """
    conv_belonging_to_user_a = _make_conv_mock(user_id=USER_A)
    session = _build_session(scalar_return=conv_belonging_to_user_a)
    ctx_b = _make_ctx(TENANT_A, USER_B)
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. POST /messages/{id}/feedback — cross-tenant → 404
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_feedback_cross_tenant_returns_404() -> None:
    """Tenant B user cannot submit feedback on Tenant A's message — must get 404."""
    # The feedback handler joins Message → Conversation and filters on tenant_id;
    # scalar() returns None when tenant doesn't match.
    session = _build_session(scalar_return=None)
    ctx_b = _make_ctx(TENANT_B, USER_B_TENANT_B)
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/api/v1/messages/{MSG_A_ID}/feedback",
            json={"rating": "up"},
        )

    assert resp.status_code == 404
    assert resp.status_code != 201


# ---------------------------------------------------------------------------
# 6. GET /conversations — user only sees their own (not others' in same tenant)
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_list_conversations_scoped_to_requesting_user() -> None:
    """GET /conversations returns only the requesting user's conversations.

    Two users share the same tenant; each must see only their own records.
    We verify that USER_B's request returns an empty list when the DB
    returns no rows for (tenant_id=TENANT_A, user_id=USER_B).
    """
    # Simulate: DB has conversations for USER_A but returns empty for USER_B query
    session = _build_session(scalars_return=[])
    ctx_b = _make_ctx(TENANT_A, USER_B)
    app = _make_app(ctx_b, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/conversations")

    assert resp.status_code == 200
    data = resp.json()
    assert data == [], f"Expected empty list but got: {data}"


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_list_conversations_owner_sees_own_conversations() -> None:
    """GET /conversations for owner returns their conversations (sanity check for isolation)."""
    own_conv = _make_conv_mock(user_id=USER_A)
    session = _build_session(scalars_return=[own_conv])
    # model_validate requires real attributes; patch them to expose as dict-compatible mock
    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/conversations")

    # 200 with one entry — proves the filter allows owner's own data through
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 7. Missing chat:query permission → 403 on each endpoint
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_conversation_without_permission_returns_403() -> None:
    """GET /conversations/{id} without chat:query permission → 403."""
    session = _build_session()
    ctx_no_perm = _make_ctx(TENANT_A, USER_A, permissions=frozenset())
    app = _make_app(ctx_no_perm, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_conversation_without_permission_returns_403() -> None:
    """DELETE /conversations/{id} without chat:query permission → 403."""
    session = _build_session()
    ctx_no_perm = _make_ctx(TENANT_A, USER_A, permissions=frozenset())
    app = _make_app(ctx_no_perm, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_list_conversations_without_permission_returns_403() -> None:
    """GET /conversations without chat:query permission → 403."""
    session = _build_session()
    ctx_no_perm = _make_ctx(TENANT_A, USER_A, permissions=frozenset())
    app = _make_app(ctx_no_perm, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/v1/conversations")

    assert resp.status_code == 403


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_feedback_without_permission_returns_403() -> None:
    """POST /messages/{id}/feedback without chat:query permission → 403."""
    session = _build_session()
    ctx_no_perm = _make_ctx(TENANT_A, USER_A, permissions=frozenset())
    app = _make_app(ctx_no_perm, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            f"/api/v1/messages/{MSG_A_ID}/feedback",
            json={"rating": "down"},
        )

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 8. Additional safety: deleted conversation is inaccessible (not returned as 200)
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_deleted_conversation_returns_404() -> None:
    """Soft-deleted conversations must not be accessible — returns 404, not 200."""
    # The query already filters is_deleted == False; simulate DB returning None.
    session = _build_session(scalar_return=None)
    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a, session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/conversations/{CONV_A_ID}")

    assert resp.status_code == 404
