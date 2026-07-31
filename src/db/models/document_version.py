"""DocumentVersion model — tracks version history for documents.

Medical protocols change over time. Each version captures an immutable snapshot
of a document at a point in time. Only one version per document may have
status='active'; all prior versions are 'superseded'.

GDPR note: `metadata` JSONB may contain author information (PII). Field is
justified in docs/rodo.md under "change_summary tracking for audit trail".
Never log this column's contents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class DocumentVersion(Base, TimestampMixin):
    """One immutable snapshot of a document at a given point in time.

    Relationships:
    - `document_id` → documents.id (CASCADE delete)
    - `ingestion_job_id` → ingestion_jobs.id (SET NULL — job may be purged)
    - `superseded_by_version_id` — self-referential, points to the version
      that replaced this one (NULL = this version has not been superseded yet)

    Circular FK note: documents.current_version_id ↔ document_versions.document_id.
    Both FKs are created with DEFERRABLE INITIALLY DEFERRED so they can be
    satisfied within the same transaction (see migration 0007).
    """

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_doc_version"),
        CheckConstraint(
            "status IN ('active', 'superseded', 'draft')",
            name="ck_document_versions_status",
        ),
        # Fast lookup: "what is the active version for document X?"
        Index("ix_document_versions_document_status", "document_id", "status"),
        # Tenant-scoped listing
        Index("ix_document_versions_tenant_id", "tenant_id"),
        # Composite tenant + document — used by the repository tenant-isolation filter
        Index("ix_document_versions_tenant_document", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # DEFERRABLE so the circular FK with documents.current_version_id can be
        # inserted in one transaction.  The constraint itself is still enforced at
        # commit time.
        ForeignKey("documents.id", ondelete="CASCADE", deferrable=True, initially="DEFERRED"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'draft'"))
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Self-referential: points to the version that superseded this one.
    # NULL means this version has not been superseded.
    superseded_by_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # GDPR: may contain author (PII). Justified in docs/rodo.md.
    # Never write to application logs or Langfuse traces.
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
        comment=(
            "Arbitrary version metadata, e.g. {change_summary, author}. "
            "May contain PII — never log."
        ),
    )
