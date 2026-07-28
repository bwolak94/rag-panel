"""RAG Pipelines API router.

All write operations require `admin:pipelines` permission.
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
from src.api.schemas.pipeline import (
    PipelineCreate,
    PipelineListResponse,
    PipelineResponse,
    PipelineUpdate,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.pipeline_service import PipelineService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

_require_admin = require_permission("admin:pipelines")
_require_read = require_permission("documents:read")


def _get_service(session: AsyncSession) -> PipelineService:
    return PipelineService(session)


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=PipelineResponse,
    summary="Create a new RAG pipeline",
)
async def create_pipeline(
    body: PipelineCreate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineResponse:
    """Create a new RAG pipeline for the requesting tenant.

    The referenced llm_model_id must be of type 'llm', active, and visible to the tenant.
    """
    ip = request.client.host if request.client else None
    result = await _get_service(session).create_pipeline(body, ctx, ip)
    await session.commit()
    return result


@router.get(
    "/",
    response_model=PipelineListResponse,
    summary="List RAG pipelines for the current tenant",
)
async def list_pipelines(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    include_inactive: bool = Query(
        default=False, description="Include inactive pipelines (requires admin:pipelines)"
    ),
) -> PipelineListResponse:
    """Return all pipelines belonging to the requesting tenant."""
    if include_inactive and not ctx.has_permission("admin:pipelines"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:pipelines permission required to list inactive pipelines",
        )
    return await _get_service(session).list_pipelines(
        ctx,
        include_inactive=include_inactive,
        offset=pagination.offset,
        limit=pagination.page_size,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{pipeline_id}",
    response_model=PipelineResponse,
    summary="Get a single RAG pipeline by ID",
)
async def get_pipeline(
    pipeline_id: UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineResponse:
    """Return a pipeline owned by the requesting tenant."""
    return await _get_service(session).get_pipeline(pipeline_id, ctx)


@router.patch(
    "/{pipeline_id}",
    response_model=PipelineResponse,
    summary="Update a RAG pipeline",
)
async def update_pipeline(
    pipeline_id: UUID,
    body: PipelineUpdate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> PipelineResponse:
    """Partially update a pipeline owned by the requesting tenant."""
    ip = request.client.host if request.client else None
    result = await _get_service(session).update_pipeline(pipeline_id, body, ctx, ip)
    await session.commit()
    return result


@router.delete(
    "/{pipeline_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a RAG pipeline (hard delete)",
)
async def delete_pipeline(
    pipeline_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    """Hard-delete a pipeline owned by the requesting tenant."""
    ip = request.client.host if request.client else None
    await _get_service(session).delete_pipeline(pipeline_id, ctx, ip)
    await session.commit()
