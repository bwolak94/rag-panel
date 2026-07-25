"""IDOR security tests for Document endpoints.

Verifies that cross-tenant document access is denied.
Marked as tenant_isolation so they run with the /tenant-isolation-check skill.
"""

from __future__ import annotations

import uuid
from datetime import UTC
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
USER_B = uuid.uuid4()
COLLECTION_A = uuid.uuid4()
DOC_B_ID = uuid.uuid4()


def _make_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub=f"kc-{user_id}",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:read", "documents:upload", "documents:manage"}),
        allowed_collection_ids=frozenset({COLLECTION_A}),
        writable_collection_ids=frozenset({COLLECTION_A}),
    )


def _make_doc_from_tenant_b() -> MagicMock:
    doc = MagicMock()
    doc.id = DOC_B_ID
    doc.tenant_id = TENANT_B
    doc.collection_id = uuid.uuid4()
    doc.title = "B's secret doc"
    doc.original_filename = "secret.pdf"
    doc.mime_type = "application/pdf"
    doc.size_bytes = 1024
    doc.sha256 = "b" * 64
    doc.status = "ready"
    doc.category = None
    doc.tags = []
    doc.language = None
    doc.uploaded_by = USER_B
    doc.validation_result = None
    from datetime import datetime

    doc.created_at = datetime.now(UTC)
    doc.updated_at = datetime.now(UTC)
    return doc


def _make_app(ctx: UserContext) -> Any:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock(spec=AsyncSession)
        session.commit = AsyncMock()
        yield session

    app.dependency_overrides[get_current_ctx] = lambda: ctx
    app.dependency_overrides[get_db_session] = _fake_session
    return app


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_get_document_from_other_tenant_returns_403() -> None:
    """GET /api/v1/documents/{doc_id} where doc belongs to tenant B → 403 for tenant A user."""
    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    # Repository returns a document belonging to TENANT_B
    with patch(
        "src.api.routers.documents.DocumentRepository.get_by_id",
        new=AsyncMock(return_value=_make_doc_from_tenant_b()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/documents/{DOC_B_ID}")

    assert resp.status_code == 403
    # Must never expose the document's existence from another tenant
    assert "secret" not in resp.text.lower()


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_document_from_other_tenant_returns_403() -> None:
    """DELETE /api/v1/documents/{doc_id} where doc belongs to tenant B → 403 for tenant A user."""
    ctx_a = _make_ctx(TENANT_A, USER_A)
    app = _make_app(ctx_a)

    with patch(
        "src.api.routers.documents.DocumentRepository.get_by_id",
        new=AsyncMock(return_value=_make_doc_from_tenant_b()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/api/v1/documents/{DOC_B_ID}")

    assert resp.status_code == 403
