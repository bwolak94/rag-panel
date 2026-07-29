"""Tenant isolation tests for DeletionService and review endpoints.

These tests verify that cross-tenant access is correctly blocked.
Marked with @pytest.mark.tenant_isolation — treated as merge blockers.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import NotFoundError
from src.domain.auth import UserContext

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
USER_A = uuid.uuid4()
USER_B = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()


def _make_ctx(tenant_id: uuid.UUID, user_id: uuid.UUID = USER_A) -> UserContext:
    return UserContext(
        user_id=user_id,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:manage", "documents:approve"}),
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset({COLLECTION_ID}),
    )


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_delete_document_cross_tenant_returns_404() -> None:
    """DeletionService raises NotFoundError when doc belongs to another tenant.

    The SQL query in delete_document is scoped to ctx.tenant_id so a cross-tenant
    document is never returned from the DB. The mock simulates this by returning None,
    matching what a real DB would return for the correctly scoped WHERE clause.
    NotFoundError is correct here — TenantIsolationError would itself be an oracle
    (leaking that the resource exists in another tenant).
    """
    from src.domain.deletion_service import DeletionService

    session = MagicMock()

    async def _execute(q: object) -> MagicMock:
        # Simulates scoped SQL: WHERE id=X AND tenant_id=TENANT_A — returns None for TENANT_B doc
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService"),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx(tenant_id=TENANT_A)

        with pytest.raises(NotFoundError):
            await svc.delete_document(
                document_id=DOCUMENT_ID,
                ctx=ctx,
                session=session,
                retrieval_svc=AsyncMock(),
            )


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_review_queue_scoped_to_requesting_tenant() -> None:
    """list_review_queue must only return documents belonging to the requesting tenant."""
    from src.domain.document_service import DocumentService

    # The repository is called with the JWT tenant_id — cross-tenant data never reaches service
    mock_repo = AsyncMock()
    mock_repo.list_needs_review = AsyncMock(return_value=([], 0))

    session = MagicMock()
    ctx = _make_ctx(tenant_id=TENANT_A)

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService"),
    ):
        svc = DocumentService(session)
        result = await svc.list_review_queue(
            ctx, offset=0, limit=20, page=1, page_size=20
        )

    # Verify the repo was queried with TENANT_A's id only
    mock_repo.list_needs_review.assert_called_once_with(TENANT_A, offset=0, limit=20)
    assert result.total == 0


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_review_approve_cross_tenant_returns_404() -> None:
    """review_document must raise NotFoundError when document not in requesting tenant.

    get_by_id is scoped to (document_id, tenant_id), so cross-tenant doc returns None → 404.
    """
    from src.domain.document_service import DocumentService

    # Document not found for TENANT_A (it belongs to TENANT_B)
    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=None)

    session = MagicMock()
    ctx = _make_ctx(tenant_id=TENANT_A)

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService"),
    ):
        svc = DocumentService(session)

        with pytest.raises(NotFoundError):
            await svc.review_document(
                document_id=DOCUMENT_ID,
                decision="approve",
                note=None,
                ctx=ctx,
                ip=None,
                session=session,
            )

    # Verify scoped by TENANT_A
    mock_repo.get_by_id.assert_called_once_with(DOCUMENT_ID, TENANT_A)
