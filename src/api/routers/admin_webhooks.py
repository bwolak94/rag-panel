"""Admin webhook management router.

Endpoints:
  POST   /api/v1/admin/webhooks                        register webhook
  GET    /api/v1/admin/webhooks                        list webhooks
  GET    /api/v1/admin/webhooks/{id}                   get webhook
  PATCH  /api/v1/admin/webhooks/{id}                   update webhook
  DELETE /api/v1/admin/webhooks/{id}                   delete webhook
  POST   /api/v1/admin/webhooks/{id}/test              send test payload
  GET    /api/v1/admin/webhooks/{id}/deliveries        delivery log (last 100)
  POST   /api/v1/admin/webhooks/deliveries/{id}/retry  manual retry

All endpoints require 'admin:webhooks' permission.
tenant_id is ALWAYS from JWT context.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.webhook import (
    RegisterWebhookRequest,
    UpdateWebhookRequest,
    WebhookCreatedResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryResponse,
    WebhookListResponse,
    WebhookResponse,
    WebhookTestResponse,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.webhook_service import WebhookService

router = APIRouter(prefix="/api/v1/admin/webhooks", tags=["admin-webhooks"])

_RequireWebhooks = Annotated[None, Depends(require_permission("admin:webhooks"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


@router.post(
    "",
    response_model=WebhookCreatedResponse,
    status_code=201,
    summary="Register a new webhook (secret shown once)",
)
async def register_webhook(
    body: RegisterWebhookRequest,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookCreatedResponse:
    return await WebhookService(session).register_webhook(
        tenant_id=ctx.tenant_id,
        created_by=ctx.user_id,
        body=body,
    )


@router.get(
    "",
    response_model=WebhookListResponse,
    summary="List all webhooks for the tenant",
)
async def list_webhooks(
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookListResponse:
    return await WebhookService(session).list_webhooks(ctx.tenant_id)


@router.get(
    "/{webhook_id}",
    response_model=WebhookResponse,
    summary="Get webhook details (secret masked)",
)
async def get_webhook(
    webhook_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookResponse:
    return await WebhookService(session).get_webhook(webhook_id, ctx.tenant_id)


@router.patch(
    "/{webhook_id}",
    response_model=WebhookResponse,
    summary="Update webhook name, events, or active status",
)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: UpdateWebhookRequest,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookResponse:
    return await WebhookService(session).update_webhook(webhook_id, ctx.tenant_id, body)


@router.delete(
    "/{webhook_id}",
    status_code=204,
    summary="Delete a webhook and all its delivery history",
)
async def delete_webhook(
    webhook_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> None:
    await WebhookService(session).delete_webhook(webhook_id, ctx.tenant_id)


@router.post(
    "/{webhook_id}/test",
    response_model=WebhookTestResponse,
    summary="Send a test payload to verify webhook connectivity",
)
async def test_webhook(
    webhook_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookTestResponse:
    return await WebhookService(session).send_test(webhook_id, ctx.tenant_id)


@router.get(
    "/{webhook_id}/deliveries",
    response_model=WebhookDeliveryListResponse,
    summary="Delivery log for a webhook (last 100 entries)",
)
async def list_deliveries(
    webhook_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookDeliveryListResponse:
    return await WebhookService(session).list_deliveries(webhook_id, ctx.tenant_id)


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=WebhookDeliveryResponse,
    summary="Manually retry a failed delivery",
)
async def retry_delivery(
    delivery_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireWebhooks,
    session: _Session,
) -> WebhookDeliveryResponse:
    return await WebhookService(session).retry_delivery(delivery_id, ctx.tenant_id)
