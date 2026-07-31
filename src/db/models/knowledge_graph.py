"""ORM models for the Graph RAG knowledge graph.

MedicalEntity — a normalized medical concept extracted from a document chunk
    (drug, condition, procedure, ICD code, anatomy term).
EntityRelation — a directed typed edge between two MedicalEntity rows extracted
    from the same document.

Security / GDPR:
- `surface_forms` stores exact textual occurrences — may contain medication names
  and anatomical terms but never PII (patient identifiers, diagnoses attributed to
  a person).  Do NOT log column values.
- `evidence_text_hash` is a SHA-256 hex digest of the supporting sentence;
  the sentence itself is NOT stored.
- Every row is scoped to `tenant_id`; all queries MUST filter on it.
- CASCADE delete on `document_id` so rows are removed when a document is deleted
  (GDPR Art. 17 — handled by DeletionService via Document cascade).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.models.base import Base, TimestampMixin


class MedicalEntity(Base, TimestampMixin):
    """A normalized medical concept extracted from one or more document chunks.

    canonical_name is the normalized entity name (e.g. "metformin hydrochloride").
    surface_forms stores the exact strings as they appear in source text (e.g.
    ["metformin", "Metformin", "Glucophage"]).  This field is GDPR-sensitive
    (text derived from ingested documents) — never log its contents.

    chunk_ids records which Qdrant point IDs (UUIDs) mention this entity so that
    the query graph can cross-reference graph context with retrieved vector chunks.
    """

    __tablename__ = "medical_entities"
    __table_args__ = (
        # Tenant-scoped lookups — most common query pattern
        Index("ix_medical_entities_tenant_id", "tenant_id"),
        # Entity-type filter within a tenant
        Index("ix_medical_entities_tenant_type", "tenant_id", "entity_type"),
        # Document cascade invalidation
        Index("ix_medical_entities_document_id", "document_id"),
        # ICD code lookup (nullable — only set for conditions)
        Index("ix_medical_entities_icd_code", "icd_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant that owns this entity.  All queries MUST filter on this column.",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    entity_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="One of: drug | condition | procedure | icd_code | anatomy",
    )
    canonical_name: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Normalised name used for deduplication and graph joins.",
    )
    # GDPR-sensitive: textual occurrences from source documents — never log.
    surface_forms: Mapped[list[Any]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'"),
        comment="Exact surface forms as seen in source text. Do NOT log.",
    )
    icd_code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="ICD-10/11 code when entity_type is 'condition' or 'icd_code'.",
    )
    atc_code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="WHO ATC drug classification code when entity_type is 'drug'.",
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="LLM extraction confidence in [0, 1].",
    )
    # Stored as ARRAY of UUID strings rather than UUID[] because SQLAlchemy's
    # ARRAY(UUID) mapping requires explicit casting in PostgreSQL and UUIDs can
    # be round-tripped as strings without loss of information.
    chunk_ids: Mapped[list[Any]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{}'"),
        comment="Qdrant point IDs (UUID strings) of chunks that mention this entity.",
    )


class EntityRelation(Base, TimestampMixin):
    """A directed typed relationship between two MedicalEntity rows.

    Extracted by a second LLM call per chunk batch.  The supporting sentence
    is NOT stored; only its SHA-256 hex digest is kept so the relationship can
    be traced back to a specific text span without storing the actual content.

    Tenant isolation: every row is scoped to `tenant_id`.  Both
    source_entity_id and target_entity_id MUST belong to the same tenant —
    this is enforced at the application layer in node_extract_entities.
    """

    __tablename__ = "entity_relations"
    __table_args__ = (
        Index("ix_entity_relations_tenant_id", "tenant_id"),
        Index("ix_entity_relations_source_entity_id", "source_entity_id"),
        Index("ix_entity_relations_target_entity_id", "target_entity_id"),
        Index("ix_entity_relations_document_id", "document_id"),
        Index("ix_entity_relations_relation_type", "tenant_id", "relation_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant scope — mandatory filter on every query.",
    )
    source_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medical_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("medical_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    relation_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment=("One of: treats | contraindicated_with | interacts_with | causes | diagnosed_by"),
    )
    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="LLM relation-extraction confidence in [0, 1].",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    evidence_text_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "SHA-256 hex digest of the sentence that supports this relation. "
            "The sentence itself is NOT stored (GDPR)."
        ),
    )
