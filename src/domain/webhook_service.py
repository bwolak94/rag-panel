"""WebhookService — tenant webhook management, delivery, and retry logic.

Security:
- URL must be HTTPS — validated at registration.
- Secret stored Fernet-encrypted; plaintext used only for HMAC signing.
- Secret never logged, never returned after creation.
- tenant_id always from caller context — never from request body.
- Payload contains only IDs and metadata — no document content or PII.
- Response body truncated to 500 chars to avoid storing external data.
- After 10 consecutive failures the webhook is auto-disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.exceptions import NotFoundError
from src.db.models.webhook import Webhook, WebhookDelivery
from src.domain.schemas.webhook import (
    RegisterWebhookRequest,
    UpdateWebhookRequest,
    WebhookCreatedResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookListResponse,
    WebhookResponse,
    WebhookTestResponse,
)

logger = structlog.get_logger(__name__)

_MAX_FAILURES = 10
_DELIVERY_TIMEOUT_SECONDS = 10
_RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)]
_MAX_RESPONSE_BODY_CHARS = 500


def _fernet() -> Fernet:
    """Return a Fernet instance keyed from SECRET_KEY (padded/hashed to 32 bytes)."""
    raw = settings.SECRET_KEY.encode()
    key_bytes = hashlib.sha256(raw).digest()
    import base64

    return Fernet(base64.urlsafe_b64encode(key_bytes))


def _encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Failed to decrypt webhook secret") from exc


def _sign_payload(secret_plaintext: str, body: bytes) -> str:
    """Return 'sha256=<hex>' HMAC-SHA256 signature."""
    sig = hmac.new(secret_plaintext.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _webhook_to_response(obj: Webhook, *, mask_secret: bool = True) -> WebhookResponse:
    return WebhookResponse(
        id=obj.id,
        tenant_id=obj.tenant_id,
        name=obj.name,
        url=obj.url,
        secret="***" if mask_secret else obj.secret,
        events=list(obj.events),
        is_active=obj.is_active,
        failure_count=obj.failure_count,
        last_triggered_at=obj.last_triggered_at,
        created_at=obj.created_at,
        created_by=obj.created_by,
    )


def _delivery_to_response(obj: WebhookDelivery) -> WebhookDeliveryResponse:
    return WebhookDeliveryResponse(
        id=obj.id,
        webhook_id=obj.webhook_id,
        event_type=obj.event_type,
        status=obj.status,
        http_status=obj.http_status,
        attempt_count=obj.attempt_count,
        next_retry_at=obj.next_retry_at,
        created_at=obj.created_at,
        delivered_at=obj.delivered_at,
    )


class WebhookService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def register_webhook(
        self,
        tenant_id: uuid.UUID,
        created_by: uuid.UUID,
        body: RegisterWebhookRequest,
    ) -> WebhookCreatedResponse:
        """Create a webhook. Returns the plaintext secret once — never again."""
        # URL HTTPS validation is already enforced by Pydantic field_validator
        plaintext_secret = secrets.token_hex(32)
        encrypted_secret = _encrypt_secret(plaintext_secret)

        obj = Webhook(
            tenant_id=tenant_id,
            name=body.name,
            url=body.url,
            secret=encrypted_secret,
            events=body.events,
            is_active=True,
            failure_count=0,
            created_at=datetime.now(UTC),
            created_by=created_by,
        )
        self._session.add(obj)
        await self._session.flush()
        logger.info("webhook.registered", webhook_id=str(obj.id), tenant_id=str(tenant_id))
        return WebhookCreatedResponse(
            id=obj.id,
            tenant_id=obj.tenant_id,
            name=obj.name,
            url=obj.url,
            secret=plaintext_secret,
            events=list(obj.events),
            is_active=obj.is_active,
            created_at=obj.created_at,
        )

    async def list_webhooks(self, tenant_id: uuid.UUID) -> WebhookListResponse:
        q = (
            select(Webhook)
            .where(Webhook.tenant_id == tenant_id)
            .order_by(Webhook.created_at.desc())
        )
        rows = (await self._session.execute(q)).scalars().all()
        count_q = select(func.count()).select_from(Webhook).where(Webhook.tenant_id == tenant_id)
        total = (await self._session.execute(count_q)).scalar_one()
        return WebhookListResponse(
            items=[_webhook_to_response(r) for r in rows],
            total=total,
        )

    async def get_webhook(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> WebhookResponse:
        obj = await self._get_or_404(webhook_id, tenant_id)
        return _webhook_to_response(obj)

    async def update_webhook(
        self,
        webhook_id: uuid.UUID,
        tenant_id: uuid.UUID,
        body: UpdateWebhookRequest,
    ) -> WebhookResponse:
        obj = await self._get_or_404(webhook_id, tenant_id)
        if body.name is not None:
            obj.name = body.name
        if body.events is not None:
            obj.events = body.events
        if body.is_active is not None:
            obj.is_active = body.is_active
        await self._session.flush()
        return _webhook_to_response(obj)

    async def delete_webhook(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        obj = await self._get_or_404(webhook_id, tenant_id)
        await self._session.delete(obj)
        await self._session.flush()

    # ── Delivery ──────────────────────────────────────────────────────────────

    async def dispatch_event(
        self,
        event_type: str,
        tenant_id: uuid.UUID,
        data: dict[str, Any],
    ) -> None:
        """Find active webhooks subscribed to event_type and schedule async deliveries."""
        q = select(Webhook).where(
            Webhook.tenant_id == tenant_id,
            Webhook.is_active.is_(True),
            Webhook.events.contains([event_type]),
        )
        webhooks = (await self._session.execute(q)).scalars().all()
        if not webhooks:
            return

        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "event": event_type,
            "tenant_id": str(tenant_id),
            "timestamp": now.isoformat(),
            "data": data,
        }

        for webhook in webhooks:
            delivery = WebhookDelivery(
                webhook_id=webhook.id,
                tenant_id=tenant_id,
                event_type=event_type,
                payload=payload,
                status="pending",
                attempt_count=0,
                created_at=now,
            )
            self._session.add(delivery)

        await self._session.flush()

        # Fire-and-forget: background tasks deliver asynchronously
        import asyncio

        for webhook in webhooks:
            # Fetch the delivery we just created
            delivery_q = (
                select(WebhookDelivery)
                .where(
                    WebhookDelivery.webhook_id == webhook.id,
                    WebhookDelivery.status == "pending",
                )
                .order_by(WebhookDelivery.created_at.desc())
                .limit(1)
            )
            found = (await self._session.execute(delivery_q)).scalar_one_or_none()
            if found:
                asyncio.create_task(  # noqa: RUF006
                    self._deliver_background(webhook.id, found.id, tenant_id)
                )

        await self._session.execute(
            update(Webhook)
            .where(Webhook.id.in_([w.id for w in webhooks]))
            .values(last_triggered_at=now)
        )

    async def send_test(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> WebhookTestResponse:
        """Send a test payload to verify connectivity."""
        webhook = await self._get_or_404(webhook_id, tenant_id)
        now = datetime.now(UTC)
        test_payload: dict[str, Any] = {
            "event": "test",
            "tenant_id": str(tenant_id),
            "timestamp": now.isoformat(),
            "data": {"message": "This is a test delivery from the RAG platform."},
        }
        delivery = WebhookDelivery(
            webhook_id=webhook.id,
            tenant_id=tenant_id,
            event_type="test",
            payload=test_payload,
            status="pending",
            attempt_count=0,
            created_at=now,
        )
        self._session.add(delivery)
        await self._session.flush()

        import asyncio

        asyncio.create_task(  # noqa: RUF006
            self._deliver_background(webhook.id, delivery.id, tenant_id)
        )
        return WebhookTestResponse(
            delivery_id=delivery.id,
            status="pending",
            message="Test payload dispatched.",
        )

    async def list_deliveries(
        self, webhook_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> WebhookDeliveryListResponse:
        await self._get_or_404(webhook_id, tenant_id)
        q = (
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(100)
        )
        rows = (await self._session.execute(q)).scalars().all()
        count_q = (
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
        )
        total = (await self._session.execute(count_q)).scalar_one()
        return WebhookDeliveryListResponse(
            items=[_delivery_to_response(r) for r in rows],
            total=total,
        )

    async def retry_delivery(
        self, delivery_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> WebhookDeliveryResponse:
        q = select(WebhookDelivery).where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.tenant_id == tenant_id,
        )
        delivery = (await self._session.execute(q)).scalar_one_or_none()
        if delivery is None:
            raise NotFoundError(f"Delivery {delivery_id} not found")

        import asyncio

        asyncio.create_task(  # noqa: RUF006
            self._deliver_background(delivery.webhook_id, delivery.id, tenant_id)
        )
        return _delivery_to_response(delivery)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _get_or_404(self, webhook_id: uuid.UUID, tenant_id: uuid.UUID) -> Webhook:
        q = select(Webhook).where(Webhook.id == webhook_id, Webhook.tenant_id == tenant_id)
        obj = (await self._session.execute(q)).scalar_one_or_none()
        if obj is None:
            raise NotFoundError(f"Webhook {webhook_id} not found")
        return obj

    async def _deliver_background(
        self,
        webhook_id: uuid.UUID,
        delivery_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Attempt HTTP delivery; update delivery record and retry on failure."""
        from src.core.database import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            delivery_q = select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
            delivery = (await session.execute(delivery_q)).scalar_one_or_none()
            if delivery is None:
                return
            webhook_q = select(Webhook).where(Webhook.id == webhook_id)
            webhook = (await session.execute(webhook_q)).scalar_one_or_none()
            if webhook is None or not webhook.is_active:
                return

            try:
                plaintext_secret = _decrypt_secret(webhook.secret)
            except RuntimeError:
                logger.error("webhook.secret_decrypt_failed", webhook_id=str(webhook_id))
                return

            body_bytes = json.dumps(delivery.payload, default=str).encode()
            signature = _sign_payload(plaintext_secret, body_bytes)
            now = datetime.now(UTC)

            try:
                async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        webhook.url,
                        content=body_bytes,
                        headers={
                            "Content-Type": "application/json",
                            "X-RAG-Signature": signature,
                            "X-RAG-Event": delivery.event_type,
                        },
                    )
                http_status = resp.status_code
                response_body = resp.text[:_MAX_RESPONSE_BODY_CHARS]
                success = 200 <= http_status < 300
            except Exception as exc:
                http_status = None
                response_body = str(exc)[:_MAX_RESPONSE_BODY_CHARS]
                success = False

            attempt_count = delivery.attempt_count + 1

            if success:
                await session.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.id == delivery_id)
                    .values(
                        status="success",
                        http_status=http_status,
                        response_body=response_body,
                        attempt_count=attempt_count,
                        delivered_at=now,
                        next_retry_at=None,
                    )
                )
                await session.execute(
                    update(Webhook).where(Webhook.id == webhook_id).values(failure_count=0)
                )
            else:
                next_retry_at: datetime | None = None
                new_status = "failed"
                if attempt_count < len(_RETRY_DELAYS):
                    next_retry_at = now + _RETRY_DELAYS[attempt_count - 1]
                    new_status = "retrying"

                await session.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.id == delivery_id)
                    .values(
                        status=new_status,
                        http_status=http_status,
                        response_body=response_body,
                        attempt_count=attempt_count,
                        next_retry_at=next_retry_at,
                    )
                )
                new_failure_count = webhook.failure_count + 1
                if new_failure_count >= _MAX_FAILURES:
                    await session.execute(
                        update(Webhook)
                        .where(Webhook.id == webhook_id)
                        .values(failure_count=new_failure_count, is_active=False)
                    )
                    logger.warning(
                        "webhook.auto_disabled",
                        webhook_id=str(webhook_id),
                        tenant_id=str(tenant_id),
                    )
                else:
                    await session.execute(
                        update(Webhook)
                        .where(Webhook.id == webhook_id)
                        .values(failure_count=new_failure_count)
                    )

            await session.commit()
            logger.info(
                "webhook.delivered" if success else "webhook.delivery_failed",
                webhook_id=str(webhook_id),
                delivery_id=str(delivery_id),
                attempt=attempt_count,
            )
