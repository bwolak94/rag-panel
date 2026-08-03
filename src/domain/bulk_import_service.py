"""BulkImportService — ZIP upload and MinIO bucket sync for bulk document ingest.

Security:
- ZIP bomb protection: reject archives where uncompressed:compressed ratio > 100:1.
- Path traversal protection: sanitize each entry filename via pathlib.Path.name.
- Max file count enforced before any I/O (configurable, default 200).
- Temp dir cleaned up on success and error (try/finally).
- tenant_id and collection_id always from caller context — never from ZIP content.
- All files go through existing ingest pipeline (PII scan, dedup, validation).
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.exceptions import DomainValidationError, NotFoundError
from src.db.models.bulk_import_job import BulkImportJob
from src.db.models.collection import Collection
from src.domain.schemas.bulk_import import (
    BulkImportStartResponse,
    BulkImportStatusResponse,
    FailedFileDetail,
)

logger = structlog.get_logger(__name__)

_MAX_ZIP_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_FILES_PER_IMPORT = 200
_ZIP_BOMB_RATIO = 100  # uncompressed:compressed — reject if exceeded
_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".pptx", ".html"}


def _sanitize_filename(raw: str) -> str:
    """Strip directory components and normalize Unicode."""
    name = Path(raw).name
    name = unicodedata.normalize("NFC", name)
    # Replace path separators that may survive on Windows
    name = name.replace("/", "_").replace("\\", "_")
    return name


def _check_zip_bomb(zf: zipfile.ZipFile, compressed_size: int) -> None:
    """Raise DomainValidationError if the archive decompression ratio is too high."""
    uncompressed_size = sum(i.file_size for i in zf.infolist())
    if compressed_size > 0 and uncompressed_size / compressed_size > _ZIP_BOMB_RATIO:
        raise DomainValidationError(
            f"ZIP archive rejected: decompression ratio exceeds {_ZIP_BOMB_RATIO}:1 "
            "(possible ZIP bomb)"
        )


def _check_path_traversal(entry_name: str) -> bool:
    """Return True if the entry name is safe (no traversal)."""
    if entry_name.startswith("/") or entry_name.startswith("\\"):
        return False
    parts = Path(entry_name).parts
    return ".." not in parts


class BulkImportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_zip_import(
        self,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
        created_by: uuid.UUID,
        archive_bytes: bytes,
    ) -> BulkImportStartResponse:
        """Validate ZIP, create job record, enqueue files asynchronously."""
        await self._assert_collection_owned(collection_id, tenant_id)

        if len(archive_bytes) > _MAX_ZIP_SIZE_BYTES:
            raise DomainValidationError(
                f"Archive exceeds maximum size of {_MAX_ZIP_SIZE_BYTES // 1024 // 1024} MB"
            )

        # Open and validate the archive before creating the job
        try:
            zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except zipfile.BadZipFile as exc:
            raise DomainValidationError("Invalid ZIP archive") from exc

        with zf:
            _check_zip_bomb(zf, len(archive_bytes))
            entries = [e for e in zf.infolist() if not e.is_dir()]

        if len(entries) > _MAX_FILES_PER_IMPORT:
            raise DomainValidationError(
                f"Archive contains {len(entries)} files; maximum is {_MAX_FILES_PER_IMPORT}"
            )

        job = BulkImportJob(
            tenant_id=tenant_id,
            collection_id=collection_id,
            created_by=created_by,
            source_type="zip_upload",
            total_files=len(entries),
            queued_files=0,
            status="processing",
            created_at=datetime.now(UTC),
        )
        self._session.add(job)
        await self._session.flush()

        job_id = job.id
        logger.info(
            "bulk_import.zip_started",
            job_id=str(job_id),
            tenant_id=str(tenant_id),
            file_count=len(entries),
        )

        asyncio.create_task(  # noqa: RUF006
            self._process_zip_background(job_id, collection_id, tenant_id, archive_bytes)
        )

        return BulkImportStartResponse(
            bulk_import_job_id=job_id,
            total_files=len(entries),
            status="processing",
            status_url=f"/api/v1/collections/{collection_id}/bulk-import/{job_id}/status",
        )

    async def create_bucket_sync(
        self,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
        created_by: uuid.UUID,
    ) -> BulkImportStartResponse:
        """Scan MinIO prefix for unindexed files and enqueue them."""
        await self._assert_collection_owned(collection_id, tenant_id)

        job = BulkImportJob(
            tenant_id=tenant_id,
            collection_id=collection_id,
            created_by=created_by,
            source_type="bucket_sync",
            total_files=0,
            queued_files=0,
            status="processing",
            created_at=datetime.now(UTC),
        )
        self._session.add(job)
        await self._session.flush()

        job_id = job.id
        logger.info(
            "bulk_import.bucket_sync_started",
            job_id=str(job_id),
            tenant_id=str(tenant_id),
        )

        asyncio.create_task(  # noqa: RUF006
            self._process_bucket_sync_background(job_id, collection_id, tenant_id)
        )

        return BulkImportStartResponse(
            bulk_import_job_id=job_id,
            total_files=0,
            status="processing",
            status_url=f"/api/v1/collections/{collection_id}/bulk-import/{job_id}/status",
        )

    async def get_job_status(
        self, job_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> BulkImportStatusResponse:
        q = select(BulkImportJob).where(
            BulkImportJob.id == job_id,
            BulkImportJob.tenant_id == tenant_id,
        )
        job = (await self._session.execute(q)).scalar_one_or_none()
        if job is None:
            raise NotFoundError(f"Bulk import job {job_id} not found")

        failed_details: list[FailedFileDetail] = []
        if job.error_summary:
            for item in job.error_summary:
                if isinstance(item, dict):
                    failed_details.append(
                        FailedFileDetail(
                            filename=item.get("filename", ""),
                            error=item.get("error", ""),
                        )
                    )

        return BulkImportStatusResponse(
            id=job.id,
            status=job.status,
            source_type=job.source_type,
            total_files=job.total_files,
            queued_files=job.queued_files,
            succeeded_files=job.succeeded_files,
            failed_files=job.failed_files,
            skipped_files=job.skipped_files,
            failed_details=failed_details,
            created_at=job.created_at,
            completed_at=job.completed_at,
        )

    # ── Background workers ────────────────────────────────────────────────────

    async def _process_zip_background(
        self,
        job_id: uuid.UUID,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
        archive_bytes: bytes,
    ) -> None:
        from src.core.database import AsyncSessionLocal

        errors: list[dict[str, Any]] = []
        queued = 0

        with tempfile.TemporaryDirectory(prefix=f"bulk_import_{job_id}_") as tmpdir:
            try:
                zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
                with zf:
                    entries = [e for e in zf.infolist() if not e.is_dir()]
                    for entry in entries:
                        raw_name = entry.filename

                        if not _check_path_traversal(raw_name):
                            errors.append(
                                {"filename": raw_name, "error": "Path traversal rejected"}
                            )
                            continue

                        safe_name = _sanitize_filename(raw_name)
                        ext = Path(safe_name).suffix.lower()

                        if ext not in _ALLOWED_EXTENSIONS:
                            errors.append(
                                {
                                    "filename": safe_name,
                                    "error": f"Unsupported file type: {ext}",
                                }
                            )
                            continue

                        if entry.file_size > _MAX_FILE_SIZE_BYTES:
                            errors.append(
                                {
                                    "filename": safe_name,
                                    "error": "File exceeds 100 MB limit",
                                }
                            )
                            continue

                        dest_path = Path(tmpdir) / safe_name
                        try:
                            data = zf.read(entry.filename)
                            dest_path.write_bytes(data)
                        except Exception as exc:
                            errors.append({"filename": safe_name, "error": str(exc)})
                            continue

                        await self._enqueue_file(safe_name, data, collection_id, tenant_id)
                        queued += 1
            except Exception as exc:
                logger.error(
                    "bulk_import.zip_processing_error",
                    job_id=str(job_id),
                    error=str(exc),
                )

        now = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(BulkImportJob)
                .where(BulkImportJob.id == job_id)
                .values(
                    queued_files=queued,
                    failed_files=len(errors),
                    status="completed" if not errors or queued > 0 else "failed",
                    error_summary=errors or None,
                    completed_at=now,
                )
            )
            await session.commit()
        logger.info(
            "bulk_import.zip_completed",
            job_id=str(job_id),
            queued=queued,
            errors=len(errors),
        )

    async def _process_bucket_sync_background(
        self,
        job_id: uuid.UUID,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """List objects in the MinIO prefix for this tenant/collection and enqueue new ones."""
        from src.core.clients.minio_client import get_minio_client
        from src.core.database import AsyncSessionLocal
        from src.db.models.document import Document

        client = get_minio_client()
        prefix = f"{tenant_id}/{collection_id}/"

        errors: list[dict[str, Any]] = []
        queued = 0
        total = 0

        try:
            loop = asyncio.get_running_loop()
            objects = await loop.run_in_executor(
                None,
                lambda: list(client.list_objects("rag-documents", prefix=prefix, recursive=True)),
            )
            total = len(objects)

            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(BulkImportJob)
                    .where(BulkImportJob.id == job_id)
                    .values(total_files=total)
                )
                await session.commit()

            for obj in objects:
                object_name = obj.object_name or ""
                filename = Path(object_name).name
                ext = Path(filename).suffix.lower()

                if ext not in _ALLOWED_EXTENSIONS:
                    errors.append({"filename": filename, "error": f"Unsupported type: {ext}"})
                    continue

                # Check if already indexed
                async with AsyncSessionLocal() as session:
                    existing_q = select(Document).where(
                        Document.tenant_id == tenant_id,
                        Document.collection_id == collection_id,
                        Document.minio_key == object_name,
                    )
                    existing = (await session.execute(existing_q)).scalar_one_or_none()

                if existing is not None:
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(BulkImportJob)
                            .where(BulkImportJob.id == job_id)
                            .values(skipped_files=BulkImportJob.skipped_files + 1)
                        )
                        await session.commit()
                    continue

                # Enqueue via existing ingest mechanism — Redis Streams event
                await self._enqueue_minio_object(object_name, filename, collection_id, tenant_id)
                queued += 1

        except Exception as exc:
            logger.error(
                "bulk_import.bucket_sync_error",
                job_id=str(job_id),
                error=str(exc),
            )
            errors.append({"filename": "_bucket_sync", "error": str(exc)})

        now = datetime.now(UTC)
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(BulkImportJob)
                .where(BulkImportJob.id == job_id)
                .values(
                    total_files=total,
                    queued_files=queued,
                    failed_files=len(errors),
                    status="completed",
                    error_summary=errors or None,
                    completed_at=now,
                )
            )
            await session.commit()

    async def _enqueue_file(
        self,
        filename: str,
        data: bytes,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Upload file to MinIO and publish Redis Streams ingest event."""
        import io as _io

        from src.core.clients.minio_client import get_minio_client
        from src.core.clients.redis_client import get_redis_client

        client = get_minio_client()
        object_key = f"{tenant_id}/{collection_id}/{uuid.uuid4()}_{filename}"
        bucket = "rag-documents"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: client.put_object(
                bucket,
                object_key,
                _io.BytesIO(data),
                length=len(data),
                content_type="application/octet-stream",
            ),
        )

        redis = get_redis_client()
        await redis.xadd(
            "ingest:events",
            {
                "event": "document.uploaded",
                "tenant_id": str(tenant_id),
                "collection_id": str(collection_id),
                "object_key": object_key,
                "filename": filename,
                "source": "bulk_import",
            },
        )

    async def _enqueue_minio_object(
        self,
        object_name: str,
        filename: str,
        collection_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> None:
        """Publish a Redis Streams ingest event for an already-uploaded MinIO object."""
        from src.core.clients.redis_client import get_redis_client

        redis = get_redis_client()
        await redis.xadd(
            "ingest:events",
            {
                "event": "document.uploaded",
                "tenant_id": str(tenant_id),
                "collection_id": str(collection_id),
                "object_key": object_name,
                "filename": filename,
                "source": "bucket_sync",
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _assert_collection_owned(
        self, collection_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        q = select(Collection).where(
            Collection.id == collection_id,
            Collection.tenant_id == tenant_id,
        )
        collection = (await self._session.execute(q)).scalar_one_or_none()
        if collection is None:
            raise NotFoundError(f"Collection {collection_id} not found")
