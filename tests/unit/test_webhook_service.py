"""Unit tests for WebhookService — signature, HTTPS validation, retry logic."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.schemas.webhook import RegisterWebhookRequest
from src.domain.webhook_service import _decrypt_secret, _encrypt_secret, _sign_payload

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_register_body(url: str = "https://example.com/hook") -> RegisterWebhookRequest:
    return RegisterWebhookRequest(name="test", url=url, events=["document.ready"])


# ── HTTPS validation ───────────────────────────────────────────────────────


def test_register_rejects_http_url() -> None:
    with pytest.raises(Exception, match="HTTPS"):
        RegisterWebhookRequest(name="bad", url="http://example.com/hook", events=["document.ready"])


def test_register_accepts_https_url() -> None:
    body = RegisterWebhookRequest(
        name="ok", url="https://example.com/hook", events=["document.ready"]
    )
    assert body.url.startswith("https://")


def test_register_rejects_unknown_event_type() -> None:
    with pytest.raises(Exception, match="Unknown event"):
        RegisterWebhookRequest(
            name="bad", url="https://example.com/hook", events=["nonexistent.event"]
        )


# ── HMAC signature ─────────────────────────────────────────────────────────


def test_sign_payload_correct_hmac() -> None:
    secret = "my-secret-key"
    body = b'{"event":"document.ready"}'
    sig = _sign_payload(secret, body)
    assert sig.startswith("sha256=")
    expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected_hex}"


def test_sign_payload_different_secret_different_sig() -> None:
    body = b'{"event":"document.ready"}'
    sig1 = _sign_payload("secret-a", body)
    sig2 = _sign_payload("secret-b", body)
    assert sig1 != sig2


def test_sign_payload_different_body_different_sig() -> None:
    secret = "same-secret"
    sig1 = _sign_payload(secret, b'{"event":"document.ready"}')
    sig2 = _sign_payload(secret, b'{"event":"document.failed"}')
    assert sig1 != sig2


# ── Secret encryption ──────────────────────────────────────────────────────


def test_encrypt_decrypt_round_trip() -> None:
    plaintext = "my-super-secret"
    encrypted = _encrypt_secret(plaintext)
    assert encrypted != plaintext
    assert _decrypt_secret(encrypted) == plaintext


def test_encrypt_produces_different_ciphertext_each_time() -> None:
    """Fernet uses random IV — same plaintext yields different ciphertext."""
    plaintext = "same-secret"
    enc1 = _encrypt_secret(plaintext)
    enc2 = _encrypt_secret(plaintext)
    assert enc1 != enc2
    assert _decrypt_secret(enc1) == plaintext
    assert _decrypt_secret(enc2) == plaintext


# ── Service register ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_webhook_returns_plaintext_secret() -> None:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # Patch Webhook model id after flush
    from src.domain.webhook_service import WebhookService

    with patch("src.domain.webhook_service._encrypt_secret", return_value="ENCRYPTED"):
        svc = WebhookService(session)
        body = _make_register_body()

        # The webhook obj is added to session — we need to mock the id
        added_objs: list = []

        def capture_add(obj: object) -> None:
            from datetime import UTC, datetime

            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
            obj.created_at = datetime.now(UTC)  # type: ignore[attr-defined]
            added_objs.append(obj)

        session.add.side_effect = capture_add
        result = await svc.register_webhook(tenant_id, user_id, body)

    assert result.secret != "***"
    assert len(result.secret) == 64  # secrets.token_hex(32) = 64 hex chars
    assert result.url == "https://example.com/hook"
    assert result.name == "test"


# ── Dispatch event ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_event_skips_when_no_active_webhooks() -> None:
    """dispatch_event should be a no-op when no webhooks match."""
    session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=mock_result)

    from src.domain.webhook_service import WebhookService

    svc = WebhookService(session)
    await svc.dispatch_event("document.ready", uuid.uuid4(), {"document_id": str(uuid.uuid4())})

    # No delivery created
    session.add.assert_not_called()


# ── Auto-disable after 10 failures ─────────────────────────────────────────


def test_max_failures_constant() -> None:
    from src.domain.webhook_service import _MAX_FAILURES

    assert _MAX_FAILURES == 10
