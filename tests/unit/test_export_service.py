"""Unit tests for ExportService — JSON generation, schema, error cases."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.schemas.export import ExportStartResponse, ExportStatusResponse

# ── _generate_json ─────────────────────────────────────────────────────────


def test_generate_json_returns_valid_bytes() -> None:
    """_generate_json produces valid UTF-8 JSON bytes with export_version field."""
    import json

    from sqlalchemy.ext.asyncio import AsyncSession

    from src.domain.export_service import ExportService

    session = MagicMock(spec=AsyncSession)
    svc = ExportService(session)

    conv_data = {
        "conversation": {
            "id": str(uuid.uuid4()),
            "title": "Test conversation",
            "created_at": datetime.now(UTC).isoformat(),
        },
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": "Hello",
                "created_at": datetime.now(UTC).isoformat(),
                "sources": [],
            }
        ],
    }

    result = svc._generate_json(conv_data)
    assert isinstance(result, bytes)

    parsed = json.loads(result.decode("utf-8"))
    assert parsed["export_version"] == "1.0"
    assert "exported_at" in parsed
    assert parsed["conversation"]["title"] == "Test conversation"


# ── ExportStartResponse schema ─────────────────────────────────────────────


def test_export_start_response_has_status_url() -> None:
    """ExportStartResponse exposes the polling URL."""
    export_id = uuid.uuid4()
    resp = ExportStartResponse(
        export_id=export_id,
        status="generating",
        format="json",
        status_url=f"/api/v1/exports/{export_id}/status",
    )
    assert resp.status == "generating"
    assert str(export_id) in resp.status_url


# ── ExportStatusResponse — ready with URL ──────────────────────────────────


def test_export_status_response_with_download_url() -> None:
    """ExportStatusResponse serialises correctly with a download URL."""
    export_id = uuid.uuid4()
    expires_at = datetime.now(UTC)
    resp = ExportStatusResponse(
        export_id=export_id,
        status="ready",
        format="pdf",
        download_url="https://minio.example.com/exports/file.pdf?sig=abc",
        download_url_expires_at=expires_at,
        expires_at=expires_at,
    )
    assert resp.status == "ready"
    assert resp.download_url is not None
    assert "minio" in resp.download_url


# ── ExportStatusResponse — not ready yet ──────────────────────────────────


def test_export_status_response_generating_has_no_url() -> None:
    """ExportStatusResponse without URL (status=generating)."""
    export_id = uuid.uuid4()
    resp = ExportStatusResponse(
        export_id=export_id,
        status="generating",
        format="json",
        download_url=None,
        download_url_expires_at=None,
        expires_at=datetime.now(UTC),
    )
    assert resp.download_url is None


# ── create_conversation_export raises on unknown conversation ──────────────


@pytest.mark.asyncio
async def test_create_conversation_export_raises_on_missing_conv() -> None:
    """create_conversation_export raises NotFoundError when conv doesn't exist in tenant."""
    from src.core.exceptions import NotFoundError
    from src.domain.auth import UserContext
    from src.domain.export_service import ExportService

    session = MagicMock()
    session.get = AsyncMock(return_value=None)  # conversation not found

    ctx = MagicMock(spec=UserContext)
    ctx.tenant_id = uuid.uuid4()
    ctx.user_id = uuid.uuid4()
    ctx.roles = []

    svc = ExportService(session)

    with pytest.raises(NotFoundError):
        await svc.create_conversation_export(uuid.uuid4(), "json", ctx)


# ── get_export_status raises on missing export ────────────────────────────


@pytest.mark.asyncio
async def test_get_export_status_raises_on_missing_export() -> None:
    """get_export_status raises NotFoundError when export doesn't exist."""
    from src.core.exceptions import NotFoundError
    from src.domain.auth import UserContext
    from src.domain.export_service import ExportService

    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    ctx = MagicMock(spec=UserContext)
    ctx.tenant_id = uuid.uuid4()
    ctx.user_id = uuid.uuid4()
    ctx.roles = []

    svc = ExportService(session)

    with pytest.raises(NotFoundError):
        await svc.get_export_status(uuid.uuid4(), ctx)
