"""Collections API router.

All write operations require `admin:collections` permission.
Read operations require `documents:read` (minimum: Viewer role).
tenant_id is ALWAYS sourced from JWT context — never from body or query params.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.schemas.collection import (
    CollectionCreate,
    CollectionListResponse,
    CollectionResponse,
    CollectionUpdate,
)
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.collection_service import CollectionService
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/collections", tags=["collections"])

_require_admin = require_permission("admin:collections")
_require_read = require_permission("documents:read")


def _get_service(session: AsyncSession) -> CollectionService:
    return CollectionService(session, RetrievalService())


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    response_model=CollectionResponse,
    summary="Create a new collection",
)
async def create_collection(
    body: CollectionCreate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionResponse:
    ip = request.client.host if request.client else None
    return await _get_service(session).create_collection(body, ctx, ip)


@router.get(
    "/",
    response_model=CollectionListResponse,
    summary="List collections accessible to the current user",
)
async def list_collections(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    include_inactive: bool = Query(
        default=False, description="Include archived collections (requires admin:collections)"
    ),
) -> CollectionListResponse:
    if include_inactive and not ctx.has_permission("admin:collections"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin:collections permission required to list inactive collections",
        )
    return await _get_service(session).list_collections(
        ctx,
        include_inactive=include_inactive,
        offset=pagination.offset,
        limit=pagination.page_size,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{collection_id}",
    response_model=CollectionResponse,
    summary="Get a single collection by ID",
)
async def get_collection(
    collection_id: str,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionResponse:
    from uuid import UUID

    try:
        cid = UUID(collection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid collection_id format",
        ) from exc

    return await _get_service(session).get_collection(cid, ctx)


@router.patch(
    "/{collection_id}",
    response_model=CollectionResponse,
    summary="Update a collection (name, description, chunk_config, validation_config, is_active)",
)
async def update_collection(
    collection_id: str,
    body: CollectionUpdate,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> CollectionResponse:
    from uuid import UUID

    try:
        cid = UUID(collection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid collection_id format",
        ) from exc

    # embedding_model_id is NOT patchable — it would corrupt Qdrant vectors
    if hasattr(body, "embedding_model_id") and "embedding_model_id" in body.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "embedding_model_id cannot be changed after creation. "
                "Use /reindex-collection to migrate to a different model."
            ),
        )

    ip = request.client.host if request.client else None
    return await _get_service(session).update_collection(cid, body, ctx, ip)


@router.delete(
    "/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a collection (async cleanup via DeletionService)",
)
async def delete_collection(
    collection_id: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _: Annotated[None, Depends(_require_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    from uuid import UUID

    try:
        cid = UUID(collection_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid collection_id format",
        ) from exc

    ip = request.client.host if request.client else None
    await _get_service(session).delete_collection(cid, ctx, ip)
