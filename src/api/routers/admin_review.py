"""Admin review panel router.

GET  /api/v1/admin/documents/review-queue          → ReviewQueueResponse
GET  /api/v1/admin/documents/{id}/review           → DocumentReviewDetail
POST /api/v1/admin/documents/{id}/approve          → ReviewDecisionResponse
POST /api/v1/admin/documents/{id}/reject           → ReviewDecisionResponse
GET  /api/v1/admin/ingestion-jobs/{id}             → IngestionJobDetail

All endpoints require permission 'documents:review'.
tenant_id is ALWAYS from JWT context — never from body/query/path.

Route order: /documents/review-queue MUST appear before /documents/{id}
to prevent FastAPI from interpreting "review-queue" as a UUID.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.admin_review import (
    ApproveDocumentRequest,
    DocumentReviewDetail,
    IngestionJobDetail,
    RejectDocumentRequest,
    ReviewDecisionResponse,
    ReviewQueueResponse,
)
from src.core.database import get_db_session
from src.domain.admin_review_service import AdminReviewService
from src.domain.auth import UserContext

router = APIRouter(prefix="/api/v1/admin", tags=["admin-review"])

_RequireReview = Annotated[None, Depends(require_permission("documents:review"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


def _get_service(session: AsyncSession) -> AdminReviewService:
    return AdminReviewService(session)


@router.get(
    "/documents/review-queue",
    response_model=ReviewQueueResponse,
    summary="List documents awaiting admin review",
)
async def get_review_queue(
    ctx: _Ctx,
    _: _RequireReview,
    session: _Session,
    collection_id: uuid.UUID | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ReviewQueueResponse:
    svc = _get_service(session)
    return await svc.get_review_queue(
        ctx,
        collection_id=collection_id,
        cursor=cursor,
        page_size=page_size,
    )


@router.get(
    "/documents/{document_id}/review",
    response_model=DocumentReviewDetail,
    summary="Get document review details with presigned preview URL",
)
async def get_document_review_detail(
    document_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireReview,
    session: _Session,
) -> DocumentReviewDetail:
    svc = _get_service(session)
    return await svc.get_document_review_detail(ctx, document_id)


@router.post(
    "/documents/{document_id}/approve",
    response_model=ReviewDecisionResponse,
    summary="Approve document and return it to the ingest queue",
)
async def approve_document(
    document_id: uuid.UUID,
    body: ApproveDocumentRequest,
    ctx: _Ctx,
    _: _RequireReview,
    session: _Session,
) -> ReviewDecisionResponse:
    svc = _get_service(session)
    result = await svc.approve_document(ctx, document_id, body)
    await session.commit()
    return result


@router.post(
    "/documents/{document_id}/reject",
    response_model=ReviewDecisionResponse,
    summary="Reject document with a mandatory reason",
)
async def reject_document(
    document_id: uuid.UUID,
    body: RejectDocumentRequest,
    ctx: _Ctx,
    _: _RequireReview,
    session: _Session,
) -> ReviewDecisionResponse:
    svc = _get_service(session)
    result = await svc.reject_document(ctx, document_id, body)
    await session.commit()
    return result


@router.get(
    "/ingestion-jobs/{job_id}",
    response_model=IngestionJobDetail,
    summary="Get full ingestion job details for admin debugging",
)
async def get_ingestion_job(
    job_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireReview,
    session: _Session,
) -> IngestionJobDetail:
    svc = _get_service(session)
    return await svc.get_ingestion_job(ctx, job_id)
