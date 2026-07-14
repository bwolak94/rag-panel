"""Documents API router.

POST   /documents              → 202 DocumentUploadResponse
GET    /documents              → 200 DocumentListResponse
GET    /documents/{id}         → 200 DocumentResponse
DELETE /documents/{id}         → 204

tenant_id always from JWT context — never from body/query/path.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import (
    assert_tenant_owns_resource,
    get_current_ctx,
    require_permission,
)
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.schemas.document import (
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadRequest,
    DocumentUploadResponse,
)
from src.core.database import get_db_session
from src.db.repositories.document_repository import DocumentRepository
from src.domain.auth import UserContext
from src.domain.document_service import DocumentService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

_require_upload = require_permission("documents:upload")
_require_read = require_permission("documents:read")
_require_manage = require_permission("documents:manage")


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentUploadResponse,
    summary="Initiate document upload — returns a presigned PUT URL",
)
async def initiate_upload(
    body: DocumentUploadRequest,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_upload)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentUploadResponse:
    client_ip = request.client.host if request.client else None
    return await DocumentService(session).initiate_upload(body=body, ctx=ctx, client_ip=client_ip)


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List documents in tenant (optionally filtered by collection)",
)
async def list_documents(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
    collection_id: Annotated[
        uuid.UUID | None, Query(description="Filter by collection")
    ] = None,
) -> DocumentListResponse:
    repo = DocumentRepository(session)
    docs, total = await repo.list_by_tenant(
        ctx.tenant_id,
        collection_id=collection_id,
        offset=pagination.offset,
        limit=pagination.page_size,
    )

    # Viewers see documents from their allowed collections only
    allowed = ctx.allowed_collection_ids
    visible = [d for d in docs if d.collection_id in allowed]

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in visible],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Get a single document by ID",
)
async def get_document(
    document_id: uuid.UUID,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_read)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DocumentResponse:
    repo = DocumentRepository(session)
    doc = await repo.get_by_id(document_id, ctx.tenant_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    assert_tenant_owns_resource(doc.tenant_id, ctx)

    response = DocumentResponse.model_validate(doc)

    # Strip validation_result unless caller has documents:manage
    if not ctx.has_permission("documents:manage"):
        response = response.model_copy(update={"validation_result": None})

    return response


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a document (status → deleted; async cleanup via DeletionService)",
)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_manage)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    repo = DocumentRepository(session)
    doc = await repo.get_by_id(document_id, ctx.tenant_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    assert_tenant_owns_resource(doc.tenant_id, ctx)

    await repo.soft_delete(document_id, ctx.tenant_id)

    from src.domain.audit_service import AuditService

    ip = request.client.host if request.client else None
    await AuditService(session).log(
        ctx=ctx,
        action="document.deleted",
        resource_type="document",
        resource_id=document_id,
        details={"collection_id": str(doc.collection_id)},
        ip=ip,
    )
    await session.commit()
    logger.info(
        "document_deleted",
        document_id=str(document_id),
        tenant_id=str(ctx.tenant_id),
    )
