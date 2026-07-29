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
    from src.api.schemas.document import DocumentResponse
    from src.domain.document_service import DocumentService

    doc = _make_doc(status="needs_review")
    mock_repo = AsyncMock()
    mock_repo.list_needs_review = AsyncMock(return_value=([doc], 1))

    session = MagicMock()

    # Build a real DocumentResponse from the mock doc (all fields are set on _make_doc)
    doc_response = DocumentResponse(
        id=doc.id,
        tenant_id=doc.tenant_id,
        collection_id=doc.collection_id,
        title=doc.title,
        original_filename=doc.original_filename,
        mime_type=doc.mime_type,
        size_bytes=doc.size_bytes,
        sha256=doc.sha256,
        status=doc.status,
        category=None,
        tags=[],
        language=None,
        uploaded_by=doc.uploaded_by,
        validation_result=None,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )

    with (
        patch("src.domain.document_service.DocumentRepository", return_value=mock_repo),
        patch("src.domain.document_service.TenantRepository"),
        patch("src.domain.document_service.AuditService"),
        patch("src.domain.document_service.DocumentResponse") as mock_resp_cls,
    ):
        mock_resp_cls.model_validate = MagicMock(return_value=doc_response)
        mock_resp_cls.return_value = doc_response

        svc = DocumentService(session)
        ctx = _make_ctx()
        result = await svc.list_review_queue(
            ctx, offset=0, limit=20, page=1, page_size=20
        )

    assert result.total == 1
    assert result.page == 1
    assert result.page_size == 20
    assert len(result.items) == 1
    mock_repo.list_needs_review.assert_called_once_with(TENANT_ID, offset=0, limit=20)


# ── review_document ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_approve_transitions_status() -> None:
    """Approving a needs_review doc transitions it to 'indexing' and resumes graph."""
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
        patch("src.graphs.ingest_graph.graph.resume_ingest_graph", new_callable=AsyncMock),
    ):
        # resume_ingest_graph is imported locally inside review_document — patch the source
        with patch(
            "src.graphs.ingest_graph.graph.build_resume_graph"
        ), patch(
            "src.domain.document_service.DocumentService.review_document",
            wraps=None,
        ):
            pass

        # Patch the local import inside the method body
        import src.graphs.ingest_graph.graph as graph_module

        original_resume = graph_module.resume_ingest_graph
        mock_resume = AsyncMock()
        graph_module.resume_ingest_graph = mock_resume  # type: ignore[assignment]
        try:
            svc = DocumentService(session)
            await svc.review_document(
                document_id=DOCUMENT_ID,
                decision="approve",
                note=None,
                ctx=ctx,
                ip="127.0.0.1",
                session=session,
            )
        finally:
            graph_module.resume_ingest_graph = original_resume  # type: ignore[assignment]

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
        await svc.review_document(
            document_id=DOCUMENT_ID,
            decision="reject",
            note="Rejected by admin",
            ctx=ctx,
            ip="127.0.0.1",
            session=session,
        )

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
                session=session,
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
                session=session,
            )
