"""Bulk document import router.

Endpoints:
  POST /api/v1/collections/{collection_id}/documents/bulk-import    ZIP upload
  GET  /api/v1/collections/{collection_id}/bulk-import/{job_id}/status  status
  POST /api/v1/collections/{collection_id}/documents/bucket-sync    MinIO sync

Requires 'documents:upload' permission.
tenant_id always from JWT context.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.auth import get_current_ctx, require_permission
from src.api.schemas.bulk_import import BulkImportStartResponse, BulkImportStatusResponse
from src.core.database import get_db_session
from src.domain.auth import UserContext
from src.domain.bulk_import_service import BulkImportService

router = APIRouter(prefix="/api/v1/collections", tags=["bulk-import"])

_RequireUpload = Annotated[None, Depends(require_permission("documents:upload"))]
_Ctx = Annotated[UserContext, Depends(get_current_ctx)]
_Session = Annotated[AsyncSession, Depends(get_db_session)]


@router.post(
    "/{collection_id}/documents/bulk-import",
    response_model=BulkImportStartResponse,
    status_code=202,
    summary="Upload a ZIP archive and enqueue all contained documents for ingest",
)
async def bulk_import_zip(
    collection_id: uuid.UUID,
    archive: UploadFile,
    ctx: _Ctx,
    _: _RequireUpload,
    session: _Session,
) -> BulkImportStartResponse:
    archive_bytes = await archive.read()
    return await BulkImportService(session).create_zip_import(
        collection_id=collection_id,
        tenant_id=ctx.tenant_id,
        created_by=ctx.user_id,
        archive_bytes=archive_bytes,
    )


@router.get(
    "/{collection_id}/bulk-import/{job_id}/status",
    response_model=BulkImportStatusResponse,
    summary="Poll the status of a bulk import job",
)
async def get_bulk_import_status(
    collection_id: uuid.UUID,
    job_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireUpload,
    session: _Session,
) -> BulkImportStatusResponse:
    return await BulkImportService(session).get_job_status(job_id, ctx.tenant_id)


@router.post(
    "/{collection_id}/documents/bucket-sync",
    response_model=BulkImportStartResponse,
    status_code=202,
    summary="Sync unindexed files from the MinIO bucket prefix for this collection",
)
async def bucket_sync(
    collection_id: uuid.UUID,
    ctx: _Ctx,
    _: _RequireUpload,
    session: _Session,
) -> BulkImportStartResponse:
    return await BulkImportService(session).create_bucket_sync(
        collection_id=collection_id,
        tenant_id=ctx.tenant_id,
        created_by=ctx.user_id,
    )
