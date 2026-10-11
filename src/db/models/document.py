"""Document model."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class Document(Base, TimestampMixin):
    """
    Uploaded document record.
    `validation_result` is GDPR-sensitive — never log its content.
    `uploaded_by` is SET NULL on user deletion (GDPR Art. 17).
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="idx_documents_sha256"),
        # ADR-024: ensures no duplicate version numbers within a version group per tenant.
        UniqueConstraint(
            "tenant_id",
            "version_group_id",
            "version_number",
            name="uq_documents_version_group_number",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    minio_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'uploaded'"), index=True
    )
    category: Mapped[str | None] = mapped_column(String(100), index=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    language: Mapped[str | None] = mapped_column(String(10), index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # GDPR-sensitive: contains extracted document content / validation scores.
    # Must NOT be written to logs, Langfuse traces, or any store other than this table.
    validation_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Versioning — points to the currently-active DocumentVersion.
    # NULL during bootstrapping (before first version is created) and while a
    # version is being prepared inside a transaction.
    # FK is DEFERRABLE to allow the circular insert with document_versions.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "document_versions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_documents_current_version_id",
        ),
        nullable=True,
    )
    # Denormalised counter — updated in the same transaction as the new version row.
    # Avoids a COUNT(*) query on every document list response.
    version_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # ADR-024: Document versioning.
    # version_group_id is shared by all versions of the same logical document.
    # Defaults to the document's own `id` for unversioned documents.
    # nullable=True here because migration 0022 adds the column in two steps:
    # first nullable (to allow backfill), then NOT NULL (via ALTER COLUMN).
    # New rows must always supply this value explicitly.
    version_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    # Sequential version number within the version group (1-based).
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # True for the latest version in a group; False for superseded versions.
    # Default True so single documents (no version_of) start as current.
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), index=True
    )
    # Semantic dedup — populated by node_semantic_dedup (migration 0008).
    dedup_embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    dedup_similarity: Mapped[float | None] = mapped_column(nullable=True)
    dedup_similar_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
