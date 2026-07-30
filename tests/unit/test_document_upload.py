"""Unit tests for Document Upload API.

DocumentService is patched — no real DB, MinIO, or Redis needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx
from src.api.schemas.document import DocumentUploadRequest, DocumentUploadResponse
from src.core.database import get_db_session
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
WRITABLE_COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()

_VALID_SHA256 = "a" * 64
_VALID_UPLOAD_BODY = {
    "collection_id": str(WRITABLE_COLLECTION_ID),
    "filename": "test.pdf",
    "mime_type": "application/pdf",
    "size_bytes": 1024,
    "sha256": _VALID_SHA256,
}


def _make_ctx(
    permissions: frozenset[str] = frozenset({"documents:upload", "documents:read"}),
    writable: frozenset[uuid.UUID] | None = None,
) -> UserContext:
    writable_ids = writable if writable is not None else frozenset({WRITABLE_COLLECTION_ID})
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="user@example.com",
        display_name="User",
        tenant_id=TENANT_ID,
        roles=frozenset({"contributor"}),
        permissions=permissions,
        allowed_collection_ids=frozenset({COLLECTION_ID, WRITABLE_COLLECTION_ID}),
        writable_collection_ids=writable_ids,
    )


def _make_upload_response() -> DocumentUploadResponse:
    return DocumentUploadResponse(
        document_id=DOCUMENT_ID,
        job_id=JOB_ID,
        upload_url="http://minio:9000/tenant-test/raw/presigned",
        minio_key=f"raw/{WRITABLE_COLLECTION_ID}/{DOCUMENT_ID}/test.pdf",
        expires_at=datetime.now(UTC),
    )


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


# ── POST /api/v1/documents ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_initiate_upload_returns_202() -> None:
    """Valid upload request → 202 with document_id, job_id, upload_url."""
    app = _make_app(_make_ctx())
    upload_response = _make_upload_response()

    with patch(
        "src.domain.document_service.DocumentService.initiate_upload",
        new=AsyncMock(return_value=upload_response),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/documents", json=_VALID_UPLOAD_BODY)

    assert resp.status_code == 202
    data = resp.json()
    assert "document_id" in data
    assert "job_id" in data
    assert "upload_url" in data
    assert "expires_at" in data


@pytest.mark.asyncio
async def test_upload_disallowed_mime_returns_422() -> None:
    """MIME type not in allowlist → 422 Pydantic validation error."""
    app = _make_app(_make_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/documents",
            json={**_VALID_UPLOAD_BODY, "mime_type": "application/x-executable"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_oversized_file_returns_422() -> None:
    """size_bytes > 100 MB → 422."""
    app = _make_app(_make_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/documents",
            json={**_VALID_UPLOAD_BODY, "size_bytes": 101 * 1024 * 1024},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_invalid_sha256_returns_422() -> None:
    """SHA-256 with wrong format → 422."""
    app = _make_app(_make_ctx())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/v1/documents",
            json={**_VALID_UPLOAD_BODY, "sha256": "not-a-hash"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_duplicate_sha256_returns_409() -> None:
    """Duplicate SHA-256 within same tenant → 409."""
    from src.core.exceptions import ConflictError

    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.initiate_upload",
        new=AsyncMock(
            side_effect=ConflictError(f"Duplicate document. existing_document_id={DOCUMENT_ID}")
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/documents", json=_VALID_UPLOAD_BODY)

    assert resp.status_code == 409
    # Generic conflict message — must NOT expose existing_document_id (oracle prevention)
    assert resp.json()["detail"] == "Resource conflict"


@pytest.mark.asyncio
async def test_upload_no_write_access_to_collection_returns_403() -> None:
    """collection_id not in ctx.writable_collection_ids → 403."""
    from src.core.exceptions import PermissionDeniedError

    app = _make_app(_make_ctx())

    with patch(
        "src.domain.document_service.DocumentService.initiate_upload",
        new=AsyncMock(side_effect=PermissionDeniedError("No write access to this collection")),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/api/v1/documents", json=_VALID_UPLOAD_BODY)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_upload_as_viewer_returns_403() -> None:
    """User with only documents:read (Viewer) cannot upload → 403."""
    app = _make_app(_make_ctx(permissions=frozenset({"documents:read"})))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/v1/documents", json=_VALID_UPLOAD_BODY)

    assert resp.status_code == 403


# ── Schema validation ─────────────────────────────────────────────────────────


def test_upload_request_valid() -> None:
    req = DocumentUploadRequest(
        collection_id=str(WRITABLE_COLLECTION_ID),
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256=_VALID_SHA256,
    )
    assert req.filename == "report.pdf"
    assert req.tags == []


def test_upload_request_sha256_wrong_length() -> None:
    with pytest.raises(ValidationError):
        DocumentUploadRequest(
            collection_id=str(WRITABLE_COLLECTION_ID),
            filename="f.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="abc",
        )


def test_upload_request_sha256_uppercase_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentUploadRequest(
            collection_id=str(WRITABLE_COLLECTION_ID),
            filename="f.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            sha256="A" * 64,
        )
