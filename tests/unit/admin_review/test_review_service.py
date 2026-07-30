"""Unit tests for AdminReviewService."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.schemas.admin_review import ApproveDocumentRequest, RejectDocumentRequest
from src.core.exceptions import InvalidDocumentStateError, NotFoundError
from src.domain.admin_review_service import AdminReviewService, _build_flag_summary
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


def _make_ctx() -> UserContext:
    return UserContext(
        user_id=USER_ID,
        tenant_id=TENANT_ID,
        keycloak_sub="sub-admin",
        email="admin@example.com",
        display_name="Admin User",
        roles=frozenset(["Admin"]),
        permissions=frozenset(["documents:review"]),
        allowed_collection_ids=frozenset([COLLECTION_ID]),
        writable_collection_ids=frozenset([COLLECTION_ID]),
    )


def _make_doc(status: str = "needs_review") -> MagicMock:
    doc = MagicMock()
    doc.id = DOCUMENT_ID
    doc.tenant_id = TENANT_ID
    doc.collection_id = COLLECTION_ID
    doc.original_filename = "test.pdf"
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/test.pdf"
    doc.size_bytes = 1024
    doc.mime_type = "application/pdf"
    doc.status = status
    doc.uploaded_by = USER_ID
    doc.created_at = datetime(2026, 7, 13, 10, 22, 0, tzinfo=UTC)
    doc.validation_result = {
        "detected_type": "report",
        "category": "medical",
        "quality_score": 0.61,
        "confidence": 0.72,
        "pii_flags": ["PESEL_NUMBER"],
        "issues": [
            {"code": "PII_DETECTED", "message": "PII found"},
            {"code": "LOW_QUALITY", "message": "Low quality score"},
        ],
    }
    return doc


def _make_job() -> MagicMock:
    job = MagicMock()
    job.id = JOB_ID
    job.document_id = DOCUMENT_ID
    job.tenant_id = TENANT_ID
    job.status = "awaiting_review"
    job.retry_count = 0
    job.started_at = datetime(2026, 7, 13, 10, 22, 5, tzinfo=UTC)
    job.completed_at = None
    job.created_at = datetime(2026, 7, 13, 10, 22, 0, tzinfo=UTC)
    job.steps = [
        {
            "stage": "fetch",
            "status": "completed",
            "started_at": None,
            "completed_at": None,
            "error": None,
            "meta": {},
        },
        {
            "stage": "pii_scan",
            "status": "awaiting_review",
            "started_at": None,
            "completed_at": None,
            "error": None,
            "meta": {"flags_count": 1},
        },
    ]
    return job


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


# ── _build_flag_summary ───────────────────────────────────────────────────────


def test_flag_summary_pii_and_low_quality() -> None:
    from src.api.schemas.admin_review import ValidationResultSchema

    vr = ValidationResultSchema(
        pii_flags=["PESEL_NUMBER"],
        quality_score=0.61,
    )
    summary = _build_flag_summary(vr)
    assert "PII" in summary
    assert "PESEL_NUMBER" in summary
    assert "0.61" in summary


def test_flag_summary_default_when_no_issues() -> None:
    from src.api.schemas.admin_review import ValidationResultSchema

    vr = ValidationResultSchema(pii_flags=[], quality_score=0.9)
    summary = _build_flag_summary(vr)
    assert "Flagged" in summary


# ── approve_document ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approve_document_sets_queued_status() -> None:
    """approve_document → status set to 'queued', reviewer info written."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()),
        patch("src.core.clients.redis_client.get_redis_client") as mock_redis_fn,
    ):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        result = await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())

    assert result.new_status == "queued"
    assert result.document_id == DOCUMENT_ID
    assert result.decided_by_id == USER_ID


@pytest.mark.asyncio
async def test_approve_calls_audit_log() -> None:
    """approve_document → AuditService.log called with action='document.approved'."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()) as mock_log,
        patch("src.core.clients.redis_client.get_redis_client") as mock_redis_fn,
    ):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())

    mock_log.assert_called_once()
    call_kwargs = mock_log.call_args.kwargs
    assert call_kwargs["action"] == "document.approved"
    assert call_kwargs["resource_id"] == DOCUMENT_ID


@pytest.mark.asyncio
async def test_approve_publishes_redis_event() -> None:
    """approve_document → Redis XADD called with action=resume."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()),
        patch("src.core.clients.redis_client.get_redis_client") as mock_redis_fn,
    ):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())

    mock_redis.xadd.assert_called_once()
    stream, fields = mock_redis.xadd.call_args[0]
    assert stream == "ingest_events"
    assert fields["action"] == "resume"
    assert fields["document_id"] == str(DOCUMENT_ID)


@pytest.mark.asyncio
async def test_approve_raises_on_redis_failure() -> None:
    """Redis publish failure → exception propagates (triggers router-level rollback)."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()),
        patch("src.core.clients.redis_client.get_redis_client") as mock_redis_fn,
        pytest.raises(RuntimeError),
    ):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock(side_effect=RuntimeError("Redis down"))
        mock_redis_fn.return_value = mock_redis

        await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())


@pytest.mark.asyncio
async def test_approve_raises_invalid_state_when_not_needs_review() -> None:
    """approve on completed document → InvalidDocumentStateError."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="completed")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        pytest.raises(InvalidDocumentStateError) as exc_info,
    ):
        await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())

    assert exc_info.value.current_status == "completed"


@pytest.mark.asyncio
async def test_approve_raises_not_found_for_missing_document() -> None:
    """Document not in tenant → NotFoundError."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=None)),
        pytest.raises(NotFoundError),
    ):
        await svc.approve_document(ctx, DOCUMENT_ID, ApproveDocumentRequest())


# ── reject_document ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reject_document_sets_rejected_status() -> None:
    """reject_document → status set to 'rejected'."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()),
    ):
        result = await svc.reject_document(
            ctx,
            DOCUMENT_ID,
            RejectDocumentRequest(reason="Contains patient PII — anonymize first"),
        )

    assert result.new_status == "rejected"
    assert result.document_id == DOCUMENT_ID


@pytest.mark.asyncio
async def test_reject_calls_audit_log() -> None:
    """reject_document → AuditService.log called with action='document.rejected'."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        patch.object(svc._doc_repo, "set_reviewed", new=AsyncMock()),
        patch.object(svc._audit, "log", new=AsyncMock()) as mock_log,
    ):
        await svc.reject_document(
            ctx,
            DOCUMENT_ID,
            RejectDocumentRequest(reason="Contains patient PII — anonymize first"),
        )

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["action"] == "document.rejected"


@pytest.mark.asyncio
async def test_reject_raises_invalid_state_when_not_needs_review() -> None:
    """reject on already rejected document → InvalidDocumentStateError."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="rejected")

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=doc)),
        pytest.raises(InvalidDocumentStateError) as exc_info,
    ):
        await svc.reject_document(
            ctx,
            DOCUMENT_ID,
            RejectDocumentRequest(reason="Should not reach here"),
        )

    assert exc_info.value.current_status == "rejected"


@pytest.mark.asyncio
async def test_reject_raises_not_found_for_missing_document() -> None:
    """Document not in tenant → NotFoundError."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    with (
        patch.object(svc._doc_repo, "get_by_id", new=AsyncMock(return_value=None)),
        pytest.raises(NotFoundError),
    ):
        await svc.reject_document(
            ctx,
            DOCUMENT_ID,
            RejectDocumentRequest(reason="Should not reach here"),
        )


# ── get_document_review_detail ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_detail_generates_presigned_url() -> None:
    """get_document_review_detail → presigned URL generated with TTL=300s."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="needs_review")
    job = _make_job()

    row = MagicMock()
    row.__getitem__ = lambda self, i: [doc, "Medical Docs", "Anna Kowalska"][i]

    with (
        patch.object(
            svc._session,
            "execute",
            new=AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row))),
        ),
        patch.object(svc._doc_repo, "get_latest_job", new=AsyncMock(return_value=job)),
        patch(
            "src.domain.admin_review_service.asyncio.to_thread",
            new=AsyncMock(return_value="https://minio/presigned?expires=300"),
        ) as mock_thread,
    ):
        result = await svc.get_document_review_detail(ctx, DOCUMENT_ID)

    assert result.preview_url == "https://minio/presigned?expires=300"
    # Verify TTL was 300 seconds — call the captured lambda with a mock MinIO client
    from datetime import timedelta

    captured_lambda = mock_thread.call_args[0][0]
    minio_mock = MagicMock()
    minio_mock.presigned_get_object = MagicMock(return_value="url")
    with patch("src.domain.admin_review_service.get_minio_client", return_value=minio_mock):
        captured_lambda()
    expires_arg = (
        minio_mock.presigned_get_object.call_args[1].get("expires")
        or minio_mock.presigned_get_object.call_args[0][2]
    )
    assert expires_arg == timedelta(seconds=300)


@pytest.mark.asyncio
async def test_review_detail_raises_not_found_for_wrong_tenant() -> None:
    """Document from different tenant → NotFoundError."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    with (
        patch.object(
            svc._session,
            "execute",
            new=AsyncMock(return_value=MagicMock(first=MagicMock(return_value=None))),
        ),
        pytest.raises(NotFoundError),
    ):
        await svc.get_document_review_detail(ctx, DOCUMENT_ID)


@pytest.mark.asyncio
async def test_review_detail_raises_invalid_state_for_non_review_doc() -> None:
    """Document in status 'completed' → InvalidDocumentStateError with current_status."""
    session = _make_session()
    ctx = _make_ctx()
    svc = AdminReviewService(session)

    doc = _make_doc(status="completed")
    row = MagicMock()
    row.__getitem__ = lambda self, i: [doc, "Medical Docs", "Anna Kowalska"][i]

    with (
        patch.object(
            svc._session,
            "execute",
            new=AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row))),
        ),
        pytest.raises(InvalidDocumentStateError) as exc_info,
    ):
        await svc.get_document_review_detail(ctx, DOCUMENT_ID)

    assert exc_info.value.current_status == "completed"
