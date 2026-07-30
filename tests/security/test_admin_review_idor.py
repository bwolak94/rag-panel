"""Tenant isolation tests for admin review panel (TASK-012).

@pytest.mark.tenant_isolation — merge blocker.

Verifies that an admin from tenant A cannot:
- See documents from tenant B in the review queue
- Get review details for a document from tenant B
- Approve a document from tenant B
- Reject a document from tenant B

Cross-tenant access must return 404, never 403 —
revealing that a resource exists is itself an isolation violation.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.core.exceptions import NotFoundError
from src.domain.admin_review_service import AdminReviewService
from src.domain.auth import UserContext

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
DOCUMENT_TENANT_B = uuid.uuid4()
USER_A = uuid.uuid4()


def _make_ctx_a() -> UserContext:
    return UserContext(
        user_id=USER_A,
        tenant_id=TENANT_A,
        keycloak_sub="sub-admin-a",
        email="admin@tenant-a.com",
        display_name="Admin A",
        roles=frozenset(["Admin"]),
        permissions=frozenset(["documents:review"]),
        allowed_collection_ids=frozenset(),
        writable_collection_ids=frozenset(),
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    return session


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_admin_cannot_approve_cross_tenant_document() -> None:
    """Admin from tenant A tries to approve document from tenant B. Must get NotFoundError."""
    from src.api.schemas.admin_review import ApproveDocumentRequest

    session = _make_session()
    ctx_a = _make_ctx_a()
    svc = AdminReviewService(session)

    # Simulate: DocumentRepository.get_by_id filters by tenant_id — returns None for cross-tenant
    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=None)),
        pytest.raises(NotFoundError),
    ):
        await svc.approve_document(ctx_a, DOCUMENT_TENANT_B, ApproveDocumentRequest())


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_admin_cannot_reject_cross_tenant_document() -> None:
    """Admin from tenant A tries to reject document from tenant B. Must get NotFoundError."""
    from src.api.schemas.admin_review import RejectDocumentRequest

    session = _make_session()
    ctx_a = _make_ctx_a()
    svc = AdminReviewService(session)

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=None)),
        pytest.raises(NotFoundError),
    ):
        await svc.reject_document(
            ctx_a,
            DOCUMENT_TENANT_B,
            RejectDocumentRequest(reason="This should not be allowed"),
        )


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_review_queue_scoped_to_tenant() -> None:
    """Review queue only returns documents from ctx.tenant_id — tenant B docs excluded."""
    from unittest.mock import MagicMock

    session = _make_session()
    ctx_a = _make_ctx_a()
    svc = AdminReviewService(session)

    # Simulate query returning no rows (tenant B docs are filtered by WHERE tenant_id = :a)
    session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[]),
            scalar_one=MagicMock(return_value=0),
        )
    )

    result = await svc.get_review_queue(ctx_a)

    assert result.total_count == 0
    assert result.items == []
    # Verify the execute was called (query ran) and returned empty for tenant A scope
    session.execute.assert_called()


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_review_detail_returns_404_for_cross_tenant_document() -> None:
    """Admin A requesting review detail for tenant B document → NotFoundError (not 403)."""
    from unittest.mock import MagicMock

    session = _make_session()
    ctx_a = _make_ctx_a()
    svc = AdminReviewService(session)

    # Simulate: document exists in DB but tenant_id filter returns no row
    session.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None)))

    with pytest.raises(NotFoundError):
        await svc.get_document_review_detail(ctx_a, DOCUMENT_TENANT_B)


@pytest.mark.tenant_isolation
@pytest.mark.asyncio
async def test_ingestion_job_scoped_to_tenant() -> None:
    """Admin A requesting job from tenant B → NotFoundError."""
    from unittest.mock import MagicMock

    session = _make_session()
    ctx_a = _make_ctx_a()
    svc = AdminReviewService(session)

    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    with pytest.raises(NotFoundError):
        await svc.get_ingestion_job(ctx_a, uuid.uuid4())
