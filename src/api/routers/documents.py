"""Documents API router.

POST   /documents                      → 202 DocumentUploadResponse
GET    /documents                      → 200 DocumentListResponse
GET    /documents/review-queue         → 200 ReviewQueueResponse  (documents:approve)
GET    /documents/{id}                 → 200 DocumentResponse
POST   /documents/{id}/review          → 204  (documents:approve)
DELETE /documents/{id}                 → 204

tenant_id always from JWT context — never from body/query/path.

Route order matters: /review-queue MUST appear before /{document_id} to prevent
FastAPI from interpreting the literal string "review-queue" as a UUID.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import (
    assert_tenant_owns_resource,
    get_current_ctx,
    require_permission,
)
from src.api.dependencies.pagination import PaginationParams, get_pagination
from src.api.dependencies.retrieval import get_retrieval_service
from src.api.schemas.document import (
    DocumentListResponse,
    DocumentResponse,
    DocumentUploadRequest,
    DocumentUploadResponse,
    ReviewDecision,
    ReviewQueueResponse,
)
from src.core.database import AsyncSessionLocal, get_db_session
from src.db.repositories.document_repository import DocumentRepository
from src.domain.auth import UserContext
from src.domain.deletion_service import DeletionService
from src.domain.document_service import DocumentService
from src.graphs.ingest_graph.graph import resume_ingest_graph
from src.retrieval.service import RetrievalService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

_require_upload = require_permission("documents:upload")
_require_read = require_permission("documents:read")
_require_manage = require_permission("documents:manage")
_require_approve = require_permission("documents:approve")


async def _run_resume_ingest_bg(
    document_id: uuid.UUID, job_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    """Background task: resume ingest graph with its own session after HTTP response."""
    async with AsyncSessionLocal() as bg_session:
        try:
            await resume_ingest_graph(
                document_id=document_id,
                job_id=job_id,
                tenant_id=tenant_id,
                session=bg_session,
            )
            await bg_session.commit()
        except Exception as exc:
            await bg_session.rollback()
            logger.warning(
                "review_document.resume_ingest_failed",
                document_id=str(document_id),
                job_id=str(job_id),
                error=str(exc),
            )


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
    collection_id: Annotated[uuid.UUID | None, Query(description="Filter by collection")] = None,
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


# IMPORTANT: /review-queue MUST be registered before /{document_id}
# to prevent FastAPI from treating the literal string "review-queue" as a UUID.
@router.get(
    "/review-queue",
    response_model=ReviewQueueResponse,
    summary="List documents awaiting admin review (documents:approve required)",
)
async def get_review_queue(
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_approve)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    pagination: Annotated[PaginationParams, Depends(get_pagination)],
) -> ReviewQueueResponse:
    docs, total = await DocumentService(session).list_review_queue(
        ctx,
        offset=pagination.offset,
        limit=pagination.page_size,
    )
    return ReviewQueueResponse(
        items=[DocumentResponse.model_validate(d) for d in docs],
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


@router.post(
    "/{document_id}/review",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Approve or reject a needs_review document (documents:approve required)",
)
async def review_document(
    document_id: uuid.UUID,
    body: ReviewDecision,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_approve)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    ip = request.client.host if request.client else None
    job_id = await DocumentService(session).review_document(
        document_id=document_id,
        decision=body.decision,
        note=body.note,
        ctx=ctx,
        ip=ip,
    )
    await session.commit()
    if job_id is not None:
        background_tasks.add_task(_run_resume_ingest_bg, document_id, job_id, ctx.tenant_id)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete a document — cascades to Qdrant vectors and MinIO object (GDPR Art. 17)",
)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(get_current_ctx)],
    _perm: Annotated[None, Depends(_require_manage)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    retrieval_svc: Annotated[RetrievalService, Depends(get_retrieval_service)],
) -> None:
    await DeletionService(session).delete_document(
        document_id=document_id,
        ctx=ctx,
        retrieval_svc=retrieval_svc,
    )
    await session.commit()
