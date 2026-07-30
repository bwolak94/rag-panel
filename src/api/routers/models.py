"""Models Registry API router.

All write operations require `admin:models` permission.
Read operations require `documents:read` (minimum: Viewer role).
tenant_id is ALWAYS sourced from JWT context — never from body or query params.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.schemas.model import (
    ModelCreate,
    ModelListResponse,
    ModelReachableResponse,
    ModelResponse,
    ModelUpdate,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.model_service import ModelService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

_require_admin = require_permission("admin:models")
_require_read = require_permission("documents:read")


def _get_service(session: AsyncSession) -> ModelService:
    return ModelService(session)


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=ModelResponse,
    summary="Register a new model in the tenant's registry",
)
async def create_model(
    body: ModelCreate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelResponse:
    """Create a tenant-scoped model entry.

    System-wide models (tenant_id=None) must be seeded via DB migrations.
    """
    ip = request.client.host if request.client else None
    result = await _get_service(session).create_model(body, ctx, ip)
    await session.commit()
    return result


@router.get(
    "/",
    response_model=ModelListResponse,
    summary="List models visible to the current tenant",
)
async def list_models(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    include_inactive: bool = Query(
        default=False, description="Include inactive models (requires admin:models)"
    ),
) -> ModelListResponse:
    """Return system-wide models and tenant-private models for the requesting tenant."""
    if include_inactive and not ctx.has_permission("admin:models"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:models permission required to list inactive models",
        )
    return await _get_service(session).list_models(
        ctx,
        include_inactive=include_inactive,
        offset=pagination.offset,
        limit=pagination.page_size,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{model_id}",
    response_model=ModelResponse,
    summary="Get a single model by ID",
)
async def get_model(
    model_id: UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelResponse:
    """Return a model if it is system-wide or owned by the requesting tenant."""
    return await _get_service(session).get_model(model_id, ctx)


@router.patch(
    "/{model_id}",
    response_model=ModelResponse,
    summary="Update a tenant-owned model",
)
async def update_model(
    model_id: UUID,
    body: ModelUpdate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelResponse:
    """Partially update a model owned by the requesting tenant.

    System-wide models (tenant_id=None) cannot be updated through this endpoint.
    """
    ip = request.client.host if request.client else None
    result = await _get_service(session).update_model(model_id, body, ctx, ip)
    await session.commit()
    return result


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate a tenant-owned model (soft-delete)",
)
async def deactivate_model(
    model_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Set is_active=False on a tenant-owned model.

    Returns 409 if the model is referenced by active pipelines.
    Returns 403 if the model is system-wide.
    """
    ip = request.client.host if request.client else None
    await _get_service(session).deactivate_model(model_id, ctx, ip)
    await session.commit()


@router.post(
    "/{model_id}/check",
    response_model=ModelReachableResponse,
    summary="Check if model endpoint is reachable (Admin only)",
)
async def check_model_reachable(
    model_id: UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelReachableResponse:
    """Probe the model's endpoint URL to verify it is reachable.

    Sends a GET request to {endpoint_url}/models with a 5-second timeout.
    Returns reachable=False on timeout, connection error, or non-2xx response.
    """
    from datetime import UTC, datetime

    reachable = await _get_service(session).validate_model_reachable(model_id, ctx)
    return ModelReachableResponse(
        model_id=model_id,
        reachable=reachable,
        checked_at=datetime.now(UTC).isoformat(),
    )
