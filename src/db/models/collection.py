"""Collection and CollectionAccess models."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class Collection(Base, TimestampMixin):
    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),
        # Fast lookup of all public collections across all tenants (platform-admin queries).
        Index("ix_collections_is_public", "is_public"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    embedding_model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("models_registry.id"), nullable=False, index=True
    )
    # chunk_config controls ingest chunking behaviour for this collection.
    # Supported keys:
    #   "strategy": "recursive" (default) | "section_aware" | "sentence" | "row"
    #       Collection-level default chunking strategy.
    #   "strategy_by_category": {"<category>": "<strategy>", ...}
    #       Per-document-category override (takes precedence over "strategy").
    #       Category comes from validation_result.category.
    #       Example: {"lab_results": "row", "clinical_note": "sentence",
    #                  "medical_guideline": "section_aware"}
    #   "chunk_size": int           — max tokens per chunk (default 512)
    #   "overlap": int              — token overlap between chunks (default 64)
    #   "min_chunk_size": int       — chunks below this size are dropped (default 64)
    #   "sentences_per_chunk": int  — sentences per chunk for "sentence" strategy (default 4)
    #   "overlap_sentences": int    — sentence overlap for "sentence" strategy (default 1)
    #   "rows_per_chunk": int       — rows per chunk for "row" strategy (default 10)
    #   "document_type_overrides": {"<doc_type>": {"chunk_size": int, "overlap": int}}
    #       Overrides chunk_size/overlap for a specific document_type value.
    chunk_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text(
            '\'{"strategy": "recursive", "chunk_size": 512, "overlap": 64, "min_chunk_size": 64}\''
        ),
    )
    validation_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'{"confidence_threshold": 0.7, "require_review": false}\''),
    )
    # search_config controls retrieval strategy for this collection.
    # Supported keys:
    #   "search_mode": "dense" (default) | "hybrid"
    #       "hybrid" enables BM25 + dense vector fusion via Reciprocal Rank Fusion.
    #   "top_k": int — override default top_k=8
    #   "score_threshold": float — override default 0.35
    search_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        server_default=text("NULL"),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Public collections are managed by a platform-admin tenant and readable by ALL tenants.
    # Non-owning tenants have read-only access — write operations are rejected in RetrievalService.
    # NULL managed_by_tenant_id means the collection is system-owned (no writable manager).
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment=(
            "When true, this collection is readable by every tenant. "
            "Write access is restricted to managed_by_tenant_id only."
        ),
    )
    managed_by_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Tenant that owns and may write this public collection. "
            "NULL = system-owned; no tenant may write directly."
        ),
    )


class CollectionAccess(Base):
    """Role-to-collection access grant."""

    __tablename__ = "collection_access"
    __table_args__ = (
        UniqueConstraint("collection_id", "role_id", name="uq_collection_access"),
        CheckConstraint("access_level IN ('read', 'write')", name="ck_collection_access_level"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    access_level: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
