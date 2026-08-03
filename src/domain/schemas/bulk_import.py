"""Pydantic schemas for the bulk document import API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, computed_field

# ── Response schemas ──────────────────────────────────────────────────────────


class BulkImportStartResponse(BaseModel):
    bulk_import_job_id: uuid.UUID
    total_files: int
    status: str
    status_url: str


class FailedFileDetail(BaseModel):
    filename: str
    error: str


class BulkImportStatusResponse(BaseModel):
    id: uuid.UUID
    status: str
    source_type: str
    total_files: int
    queued_files: int
    succeeded_files: int
    failed_files: int
    skipped_files: int
    failed_details: list[FailedFileDetail] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_pct(self) -> int:
        if self.total_files == 0:
            return 0
        done = self.succeeded_files + self.failed_files + self.skipped_files
        return min(100, int(done * 100 / self.total_files))
