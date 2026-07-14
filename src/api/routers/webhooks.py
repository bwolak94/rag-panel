"""Internal webhook router — MinIO bucket notifications.

POST /internal/minio-webhook

Security:
- NOT authenticated via JWT (no Keycloak token)
- NOT exposed through Traefik (include_in_schema=False; internal network only)
- Protected by HMAC-safe secret comparison (settings.MINIO_WEBHOOK_SECRET)
"""

from __future__ import annotations

import hmac
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.database import get_db_session
from src.core.events.minio_webhook import WebhookHandler

logger = structlog.get_logger(__name__)

webhook_router = APIRouter(tags=["internal"])


@webhook_router.post(
    "/internal/minio-webhook",
    include_in_schema=False,
    summary="MinIO ObjectCreated:Put webhook receiver",
)
async def minio_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    # Validate shared secret with timing-safe comparison
    secret = request.headers.get("X-Minio-Webhook-Secret", "")
    if not hmac.compare_digest(secret.encode(), settings.MINIO_WEBHOOK_SECRET.encode()):
        logger.warning("webhook_invalid_secret")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret")

    payload = await request.json()
    await WebhookHandler(session).handle_minio_event(payload)
    return {"status": "accepted"}
