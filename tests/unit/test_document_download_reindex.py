"""Unit tests for document download URL and reindex endpoints.

Both endpoints are tested against the service layer (mocked) and against
the API router to verify HTTP contracts.  No real DB, MinIO, or ingest graph
is instantiated.
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
from src.core.exceptions import DomainValidationError, NotFoundError, PermissionDeniedError
from src.domain.auth import UserContext
from src.domain.schemas.document import DownloadUrlResponse, ReindexResponse

# ── Fixtures ──────────────────────────────────────────────────────────────────

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()
OTHER_TENANT_ID = uuid.uuid4()


def _make_ctx(
    permissions: frozenset[str] | None = None,
    allowed_collections: frozenset[uuid.UUID] | None = None,
) -> UserContext:
    if permissions is None:
        permissions = frozenset({"documents:read", "documents:manage"})
    if allowed_collections is None:
        allowed_collections = frozenset({COLLECTION_ID})
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=TENANT_ID,
        roles=frozenset({"admin"}),
        permissions=permissions,
        allowed_collection_ids=allowed_collections,
        writable_collection_ids=allowed_collections,
    )


def _make_download_response() -> DownloadUrlResponse:
    return DownloadUrlResponse(
        download_url="http://minio:9000/tenant-test/raw/presigned?X-Amz-Signature=abc",
        expires_at=datetime.now(UTC),
    )


def _make_reindex_response() -> ReindexResponse:
    return ReindexResponse(job_id=JOB_ID)


def _make_app(ctx: UserContext) -> Any:
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


# ── GET /api/v1/documents/{id}/download ───────────────────────────────────────


@pytest.mark.asyncio
async def test_download_url_returns_200_with_presigned_url() -> None:
    """Valid request → 200 with download_url and expires_at."""
    app = _make_app(_make_ctx())
    download_resp = _make_download_response()

    with patch(
        "src.domain.document_service.DocumentService.get_download_url",
        new=AsyncMock(return_value=download_resp),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/documents/{DOCUMENT_ID}/download")

    assert resp.status_code == 200
    data = resp.json()
    assert "download_url" in data
    assert "expires_at" in data
    assert data["download_url"] == download_resp.download_url


@pytest.mark.asyncio
async def test_download_url_requires_documents_read_permission() -> None:
    """No documents:read permission → 403."""
    app = _make_app(_make_ctx(permissions=frozenset({"documents:manage"})))

    # Service is never reached; permission check fails first
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(f"/api/v1/documents/{DOCUMENT_ID}/download")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_url_not_found_returns_404() -> None:
    """Service raises NotFoundError → 404."""
    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.get_download_url",
        new=AsyncMock(side_effect=NotFoundError("Document not found")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/documents/{DOCUMENT_ID}/download")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_url_no_collection_access_returns_403() -> None:
    """Service raises PermissionDeniedError → 403."""
    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.get_download_url",
        new=AsyncMock(side_effect=PermissionDeniedError("No read access")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(f"/api/v1/documents/{DOCUMENT_ID}/download")

    assert resp.status_code == 403


# ── POST /api/v1/documents/{id}/reindex ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reindex_returns_202_with_job_id() -> None:
    """Valid reindex request → 202 with job_id."""
    app = _make_app(_make_ctx())
    reindex_resp = _make_reindex_response()

    with (
        patch(
            "src.domain.document_service.DocumentService.reindex_document",
            new=AsyncMock(return_value=reindex_resp),
        ),
        patch(
            "src.api.routers.documents._run_reindex_ingest_bg",
            new=AsyncMock(),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/api/v1/documents/{DOCUMENT_ID}/reindex")

    assert resp.status_code == 202
    data = resp.json()
    assert data["job_id"] == str(JOB_ID)


@pytest.mark.asyncio
async def test_reindex_requires_documents_manage_permission() -> None:
    """Only documents:read (no documents:manage) → 403."""
    app = _make_app(_make_ctx(permissions=frozenset({"documents:read"})))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"/api/v1/documents/{DOCUMENT_ID}/reindex")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reindex_document_not_found_returns_404() -> None:
    """Service raises NotFoundError → 404."""
    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.reindex_document",
        new=AsyncMock(side_effect=NotFoundError("Document not found")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/api/v1/documents/{DOCUMENT_ID}/reindex")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reindex_wrong_status_returns_422() -> None:
    """Service raises DomainValidationError (wrong status) → 422."""
    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.reindex_document",
        new=AsyncMock(
            side_effect=DomainValidationError(
                "Document cannot be reindexed from status 'needs_review'"
            )
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(f"/api/v1/documents/{DOCUMENT_ID}/reindex")

    assert resp.status_code == 422


# ── DocumentService unit tests (no HTTP, no DB) ───────────────────────────────


@pytest.mark.asyncio
async def test_get_download_url_wrong_tenant_returns_404() -> None:
    """get_download_url returns 404 when document does not belong to the request tenant."""
    from src.domain.document_service import DocumentService

    session = MagicMock(spec=AsyncSession)
    service = DocumentService(session)

    # repo.get_by_id filters by tenant_id; returns None for wrong tenant
    service._repo = MagicMock()
    service._repo.get_by_id = AsyncMock(return_value=None)

    ctx = _make_ctx()
    with pytest.raises(NotFoundError):
        await service.get_download_url(document_id=DOCUMENT_ID, ctx=ctx, ip=None)


@pytest.mark.asyncio
async def test_get_download_url_deleted_document_returns_404() -> None:
    """get_download_url raises NotFoundError for soft-deleted documents (GDPR Art. 17)."""
    from src.db.models.document import Document
    from src.domain.document_service import DocumentService

    session = MagicMock(spec=AsyncSession)
    service = DocumentService(session)

    doc = MagicMock(spec=Document)
    doc.status = "deleted"
    doc.collection_id = COLLECTION_ID
    doc.tenant_id = TENANT_ID

    service._repo = MagicMock()
    service._repo.get_by_id = AsyncMock(return_value=doc)

    ctx = _make_ctx()
    with pytest.raises(NotFoundError):
        await service.get_download_url(document_id=DOCUMENT_ID, ctx=ctx, ip=None)


@pytest.mark.asyncio
async def test_get_download_url_enforces_collection_access() -> None:
    """get_download_url raises PermissionDeniedError when collection not in allowed list
    and caller does not have documents:manage."""
    from src.db.models.document import Document
    from src.domain.document_service import DocumentService

    session = MagicMock(spec=AsyncSession)
    service = DocumentService(session)

    # Document belongs to a collection the user cannot access
    other_collection = uuid.uuid4()
    doc = MagicMock(spec=Document)
    doc.status = "ready"
    doc.collection_id = other_collection
    doc.tenant_id = TENANT_ID
    doc.minio_key = "raw/some/key"

    service._repo = MagicMock()
    service._repo.get_by_id = AsyncMock(return_value=doc)

    # Only documents:read — no documents:manage bypass
    ctx = _make_ctx(
        permissions=frozenset({"documents:read"}),
        allowed_collections=frozenset({COLLECTION_ID}),
    )

    with pytest.raises(PermissionDeniedError):
        await service.get_download_url(document_id=DOCUMENT_ID, ctx=ctx, ip=None)


@pytest.mark.asyncio
async def test_reindex_document_raises_for_non_terminal_status() -> None:
    """reindex_document raises DomainValidationError for documents mid-pipeline."""
    from src.db.models.document import Document
    from src.domain.document_service import DocumentService

    session = MagicMock(spec=AsyncSession)
    service = DocumentService(session)

    for non_reindexable in ("uploaded", "validating", "indexing", "needs_review"):
        doc = MagicMock(spec=Document)
        doc.status = non_reindexable
        doc.collection_id = COLLECTION_ID
        doc.tenant_id = TENANT_ID

        service._repo = MagicMock()
        service._repo.get_by_id = AsyncMock(return_value=doc)

        ctx = _make_ctx()
        with pytest.raises(DomainValidationError):
            await service.reindex_document(document_id=DOCUMENT_ID, ctx=ctx, ip=None)


@pytest.mark.asyncio
async def test_reindex_document_allowed_statuses() -> None:
    """reindex_document succeeds for ready, failed, rejected statuses."""
    from src.db.models.document import Document
    from src.db.models.ingestion_job import IngestionJob
    from src.domain.document_service import DocumentService

    for reindexable in ("ready", "failed", "rejected"):
        session = MagicMock(spec=AsyncSession)
        session.add = MagicMock()
        session.flush = AsyncMock()

        service = DocumentService(session)

        doc = MagicMock(spec=Document)
        doc.status = reindexable
        doc.collection_id = COLLECTION_ID
        doc.tenant_id = TENANT_ID

        mock_job = MagicMock(spec=IngestionJob)
        mock_job.id = JOB_ID

        service._repo = MagicMock()
        service._repo.get_by_id = AsyncMock(return_value=doc)
        service._repo.update_status = AsyncMock()
        service._audit = MagicMock()
        service._audit.log = AsyncMock()

        # IngestionJob is imported at module level in document_service
        with patch("src.domain.document_service.IngestionJob", return_value=mock_job):
            result = await service.reindex_document(
                document_id=DOCUMENT_ID, ctx=_make_ctx(), ip=None
            )

        assert result.job_id == JOB_ID
        service._repo.update_status.assert_awaited_once_with(DOCUMENT_ID, TENANT_ID, "uploaded")
