"""Unit tests for the Admin Review Queue API.

DocumentService.list_review_queue and DocumentService.review_document
are tested in isolation with mocked repositories.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import DomainValidationError, NotFoundError
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_ctx() -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-admin",
        email="admin@example.com",
        display_name="Admin",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:manage", "documents:approve", "documents:read"}),
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset({COLLECTION_ID}),
    )


def _make_doc(status: str = "needs_review") -> MagicMock:
    doc = MagicMock()
    doc.id = DOCUMENT_ID
    doc.tenant_id = TENANT_ID
    doc.collection_id = COLLECTION_ID
    doc.status = status
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/file.pdf"
    doc.title = "Test Document"
    doc.original_filename = "file.pdf"
    doc.mime_type = "application/pdf"
    doc.size_bytes = 1024
    doc.sha256 = "a" * 64
    doc.category = None
    doc.tags = []
    doc.language = None
    doc.uploaded_by = USER_ID
    doc.validation_result = None
    doc.created_at = datetime.now(UTC)
    doc.updated_at = datetime.now(UTC)
    return doc


def _make_job() -> MagicMock:
    job = MagicMock()
    job.id = JOB_ID
    job.tenant_id = TENANT_ID
    job.document_id = DOCUMENT_ID
    job.status = "awaiting_review"
    return job


# ── list_review_queue ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_review_queue_returns_needs_review_docs() -> None:
    """list_review_queue returns only needs_review documents for the tenant."""
    from src.domain.document_service import DocumentService

    doc = _make_doc(status="needs_review")
    mock_repo = AsyncMock()
    mock_repo.list_needs_review = AsyncMock(return_value=([doc], 1))

    session = MagicMock()

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService"),
    ):
        svc = DocumentService(session)
        ctx = _make_ctx()
        docs, total = await svc.list_review_queue(ctx, offset=0, limit=20)

    assert total == 1
    assert len(docs) == 1
    mock_repo.list_needs_review.assert_called_once_with(TENANT_ID, offset=0, limit=20)


# ── review_document ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_approve_transitions_status() -> None:
    """Approving a needs_review doc transitions it to 'indexing' and returns job_id."""
    from src.domain.document_service import DocumentService

    doc = _make_doc(status="needs_review")
    job = _make_job()

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=doc)
    mock_repo.set_reviewed = AsyncMock()
    mock_repo.get_latest_job = AsyncMock(return_value=job)

    mock_audit = AsyncMock()
    mock_audit.log = AsyncMock()

    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    ctx = _make_ctx()

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService", return_value=mock_audit),
    ):
        svc = DocumentService(session)
        job_id = await svc.review_document(
            document_id=DOCUMENT_ID,
            decision="approve",
            note=None,
            ctx=ctx,
            ip="127.0.0.1",
        )

    assert job_id == JOB_ID
    mock_repo.set_reviewed.assert_called_once_with(
        DOCUMENT_ID,
        TENANT_ID,
        status="indexing",
        reviewed_by=USER_ID,
        reviewed_at=mock_repo.set_reviewed.call_args.kwargs["reviewed_at"],
    )
    mock_audit.log.assert_called_once()
    assert mock_audit.log.call_args.kwargs["action"] == "document.review_approved"


@pytest.mark.asyncio
async def test_review_reject_transitions_status() -> None:
    """Rejecting a needs_review doc transitions it to 'rejected'."""
    from src.domain.document_service import DocumentService

    doc = _make_doc(status="needs_review")
    job = _make_job()

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=doc)
    mock_repo.set_reviewed = AsyncMock()
    mock_repo.get_latest_job = AsyncMock(return_value=job)

    mock_audit = AsyncMock()
    mock_audit.log = AsyncMock()

    session = MagicMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    ctx = _make_ctx()

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService", return_value=mock_audit),
    ):
        svc = DocumentService(session)
        result = await svc.review_document(
            document_id=DOCUMENT_ID,
            decision="reject",
            note="Rejected by admin",
            ctx=ctx,
            ip="127.0.0.1",
        )

    assert result is None
    mock_repo.set_reviewed.assert_called_once_with(
        DOCUMENT_ID,
        TENANT_ID,
        status="rejected",
        reviewed_by=USER_ID,
        reviewed_at=mock_repo.set_reviewed.call_args.kwargs["reviewed_at"],
    )
    mock_audit.log.assert_called_once()
    assert mock_audit.log.call_args.kwargs["action"] == "document.review_rejected"
    # Note: note text is NOT in audit log details
    assert "note" not in mock_audit.log.call_args.kwargs.get("details", {})


@pytest.mark.asyncio
async def test_review_document_not_needs_review_returns_422() -> None:
    """DomainValidationError raised if document is not in needs_review status."""
    from src.domain.document_service import DocumentService

    doc = _make_doc(status="ready")  # Not needs_review

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=doc)

    session = MagicMock()
    ctx = _make_ctx()

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService"),
    ):
        svc = DocumentService(session)

        with pytest.raises(DomainValidationError):
            await svc.review_document(
                document_id=DOCUMENT_ID,
                decision="approve",
                note=None,
                ctx=ctx,
                ip=None,
            )


@pytest.mark.asyncio
async def test_review_document_wrong_tenant_returns_404() -> None:
    """NotFoundError raised when document not found in tenant (prevents IDOR)."""
    from src.domain.document_service import DocumentService

    mock_repo = AsyncMock()
    mock_repo.get_by_id = AsyncMock(return_value=None)  # Not found for this tenant

    session = MagicMock()
    ctx = _make_ctx()

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
            )


# ── HTTP-level permission tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_review_queue_requires_approve_permission() -> None:
    """GET /api/v1/documents/review-queue without documents:approve → 403."""
    from httpx import ASGITransport, AsyncClient

    from src.api.dependencies.auth import get_current_ctx
    from src.main import create_app

    ctx_no_approve = UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-viewer",
        email="viewer@example.com",
        display_name="Viewer",
        tenant_id=TENANT_ID,
        roles=frozenset({"viewer"}),
        permissions=frozenset({"documents:read"}),  # no documents:approve
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset(),
    )

    app = create_app()
    app.dependency_overrides[get_current_ctx] = lambda: ctx_no_approve

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/documents/review-queue")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_review_requires_approve_permission() -> None:
    """POST /api/v1/documents/{id}/review without documents:approve → 403."""
    from httpx import ASGITransport, AsyncClient

    from src.api.dependencies.auth import get_current_ctx
    from src.main import create_app

    ctx_no_approve = UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-viewer",
        email="viewer@example.com",
        display_name="Viewer",
        tenant_id=TENANT_ID,
        roles=frozenset({"viewer"}),
        permissions=frozenset({"documents:read"}),  # no documents:approve
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset(),
    )

    app = create_app()
    app.dependency_overrides[get_current_ctx] = lambda: ctx_no_approve

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/documents/{DOCUMENT_ID}/review",
            json={"decision": "approve"},
        )

    assert resp.status_code == 403
