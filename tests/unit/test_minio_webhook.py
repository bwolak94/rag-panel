"""Unit tests for MinIO webhook handler and endpoint."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.events.minio_webhook import WebhookHandler

TENANT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
TENANT_SLUG = "test-clinic"


def _make_s3_record(
    bucket: str = f"tenant-{TENANT_SLUG}",
    key: str | None = None,
) -> dict:
    if key is None:
        key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/report.pdf"
    return {
        "eventName": "s3:ObjectCreated:Put",
        "s3": {
            "bucket": {"name": bucket},
            "object": {
                "key": key,
                "size": 1024,
                "contentType": "application/pdf",
            },
        },
    }


def _make_payload(
    bucket: str = f"tenant-{TENANT_SLUG}",
    key: str | None = None,
) -> dict:
    return {"Records": [_make_s3_record(bucket=bucket, key=key)]}


def _make_tenant() -> MagicMock:
    t = MagicMock()
    t.id = TENANT_ID
    t.slug = TENANT_SLUG
    return t


def _make_document() -> MagicMock:
    d = MagicMock()
    d.id = DOCUMENT_ID
    d.tenant_id = TENANT_ID
    return d


# ── WebhookHandler unit tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_event_updates_status_and_publishes() -> None:
    """Valid MinIO event → document status set to 'validating', Redis xadd called."""
    session = MagicMock(spec=AsyncSession)
    session.commit = AsyncMock()

    with (
        patch(
            "src.core.events.minio_webhook.TenantRepository.get_by_slug",
            new=AsyncMock(return_value=_make_tenant()),
        ),
        patch(
            "src.core.events.minio_webhook.DocumentRepository.get_by_id",
            new=AsyncMock(return_value=_make_document()),
        ),
        patch(
            "src.core.events.minio_webhook.DocumentRepository.update_status",
            new=AsyncMock(),
        ) as mock_update,
        patch("src.core.events.minio_webhook.get_redis_client", new=AsyncMock()) as mock_redis_fn,
    ):
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        handler = WebhookHandler(session)
        await handler.handle_minio_event(_make_payload())

    mock_update.assert_called_once_with(DOCUMENT_ID, TENANT_ID, "validating")
    mock_redis.xadd.assert_called_once()
    call_args = mock_redis.xadd.call_args
    event_data = call_args[0][1]
    assert event_data["schema_version"] == "1"
    assert event_data["event_type"] == "document.uploaded"
    assert event_data["tenant_id"] == str(TENANT_ID)
    assert event_data["document_id"] == str(DOCUMENT_ID)
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_bucket_prefix_skips_without_redis() -> None:
    """Event with bucket not starting with 'tenant-' → logged warning, no Redis."""
    session = MagicMock(spec=AsyncSession)

    with patch("src.core.events.minio_webhook.get_redis_client") as mock_redis_fn:
        handler = WebhookHandler(session)
        await handler.handle_minio_event(_make_payload(bucket="other-bucket"))

    mock_redis_fn.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_key_skips_without_redis() -> None:
    """Event with key not matching raw/{uuid}/{uuid}/... → skipped."""
    session = MagicMock(spec=AsyncSession)

    with (
        patch(
            "src.core.events.minio_webhook.TenantRepository.get_by_slug",
            new=AsyncMock(return_value=_make_tenant()),
        ),
        patch("src.core.events.minio_webhook.get_redis_client") as mock_redis_fn,
    ):
        handler = WebhookHandler(session)
        await handler.handle_minio_event(_make_payload(key="uploads/some-file.pdf"))

    mock_redis_fn.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_tenant_skips_without_redis() -> None:
    """Event with unknown tenant slug → logged error, no Redis."""
    session = MagicMock(spec=AsyncSession)

    with (
        patch(
            "src.core.events.minio_webhook.TenantRepository.get_by_slug",
            new=AsyncMock(return_value=None),
        ),
        patch("src.core.events.minio_webhook.get_redis_client") as mock_redis_fn,
    ):
        handler = WebhookHandler(session)
        await handler.handle_minio_event(_make_payload())

    mock_redis_fn.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_document_id_skips_without_redis() -> None:
    """Event with document_id not in DB → logged error, no Redis."""
    session = MagicMock(spec=AsyncSession)

    with (
        patch(
            "src.core.events.minio_webhook.TenantRepository.get_by_slug",
            new=AsyncMock(return_value=_make_tenant()),
        ),
        patch(
            "src.core.events.minio_webhook.DocumentRepository.get_by_id",
            new=AsyncMock(return_value=None),
        ),
        patch("src.core.events.minio_webhook.get_redis_client") as mock_redis_fn,
    ):
        handler = WebhookHandler(session)
        await handler.handle_minio_event(_make_payload())

    mock_redis_fn.assert_not_called()


@pytest.mark.asyncio
async def test_non_objectcreated_event_skipped() -> None:
    """Non-ObjectCreated event → ignored."""
    session = MagicMock(spec=AsyncSession)
    payload = {"Records": [{"eventName": "s3:ObjectRemoved:Delete", "s3": {}}]}

    with patch("src.core.events.minio_webhook.get_redis_client") as mock_redis_fn:
        handler = WebhookHandler(session)
        await handler.handle_minio_event(payload)

    mock_redis_fn.assert_not_called()


# ── Webhook endpoint authentication tests ─────────────────────────────────────


def _make_webhook_app() -> Any:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI

    from src.main import create_app

    app: FastAPI = create_app()

    async def _fake_session() -> AsyncGenerator[MagicMock, None]:
        session = MagicMock(spec=AsyncSession)
        session.commit = AsyncMock()
        yield session

    from src.core.database import get_db_session

    app.dependency_overrides[get_db_session] = _fake_session
    return app


@pytest.mark.asyncio
async def test_webhook_missing_secret_returns_403() -> None:
    """POST /internal/minio-webhook without secret header → 403."""
    app = _make_webhook_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/internal/minio-webhook", json={"Records": []})

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_wrong_secret_returns_403() -> None:
    """POST /internal/minio-webhook with wrong secret → 403."""
    app = _make_webhook_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/internal/minio-webhook",
            json={"Records": []},
            headers={"X-Minio-Webhook-Secret": "wrong-secret"},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_webhook_valid_secret_returns_200() -> None:
    """POST /internal/minio-webhook with correct secret → 200."""
    from src.core.config import settings

    app = _make_webhook_app()

    with patch(
        "src.api.routers.webhooks.WebhookHandler.handle_minio_event",
        new=AsyncMock(),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/internal/minio-webhook",
                json={"Records": []},
                headers={"X-Minio-Webhook-Secret": settings.MINIO_WEBHOOK_SECRET},
            )

    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
