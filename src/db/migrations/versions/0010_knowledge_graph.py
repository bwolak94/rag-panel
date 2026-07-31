"""Add medical_entities and entity_relations tables for Graph RAG.

medical_entities — normalized medical concepts (drug, condition, procedure,
    icd_code, anatomy) extracted per document chunk by node_extract_entities.
entity_relations — directed typed edges between two entities in the same
    document, produced by a second LLM call per chunk batch.

Tenant isolation:
- Both tables carry a non-FK tenant_id column (denormalised for query
  performance).  The column is NOT declared as a FK to tenants.id so that
  rows survive tenant-level administrative operations; application code is
  responsible for the isolation filter.
- document_id → documents.id with CASCADE DELETE — rows are removed when the
  document is deleted, satisfying GDPR Art. 17 via DeletionService.

Security note:
- surface_forms (ARRAY TEXT) stores exact text occurrences from source
  documents.  This column is GDPR-sensitive; never log its contents.
- evidence_text_hash stores SHA-256 hex digest only — the supporting sentence
  itself is NOT stored.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # medical_entities
    # ------------------------------------------------------------------
    op.create_table(
        "medical_entities",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            nullable=False,
            comment=(
                "Owning tenant.  Denormalised — not a FK so rows survive "
                "tenant-level operations.  All queries MUST filter on this."
            ),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("document_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "entity_type",
            sa.String(50),
            nullable=False,
            comment="One of: drug | condition | procedure | icd_code | anatomy",
        ),
        sa.Column(
            "canonical_name",
            sa.String(500),
            nullable=False,
            comment="Normalised entity name used for deduplication and graph joins.",
        ),
        sa.Column(
            "surface_forms",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
            comment=(
                "Exact textual occurrences from source documents.  GDPR-sensitive — "
                "do NOT log or expose in API responses without access control."
            ),
        ),
        sa.Column(
            "icd_code",
            sa.String(20),
            nullable=True,
            comment="ICD-10/11 code when applicable (conditions, diagnoses).",
        ),
        sa.Column(
            "atc_code",
            sa.String(20),
            nullable=True,
            comment="WHO ATC drug code when entity_type is 'drug'.",
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=False,
            comment="LLM extraction confidence [0, 1].",
        ),
        sa.Column(
            "chunk_ids",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
            comment="Qdrant point IDs (UUID strings) of chunks that mention this entity.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_medical_entities_tenant_id",
        "medical_entities",
        ["tenant_id"],
    )
    op.create_index(
        "ix_medical_entities_tenant_type",
        "medical_entities",
        ["tenant_id", "entity_type"],
    )
    op.create_index(
        "ix_medical_entities_document_id",
        "medical_entities",
        ["document_id"],
    )
    op.create_index(
        "ix_medical_entities_icd_code",
        "medical_entities",
        ["icd_code"],
    )

    # ------------------------------------------------------------------
    # entity_relations
    # ------------------------------------------------------------------
    op.create_table(
        "entity_relations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="Owning tenant.  Denormalised — all queries MUST filter on this.",
        ),
        sa.Column(
            "source_entity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medical_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            UUID(as_uuid=True),
            sa.ForeignKey("medical_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "relation_type",
            sa.String(50),
            nullable=False,
            comment=(
                "One of: treats | contraindicated_with | interacts_with | "
                "causes | diagnosed_by"
            ),
        ),
        sa.Column(
            "confidence",
            sa.Float(),
            nullable=False,
            comment="LLM relation-extraction confidence [0, 1].",
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_text_hash",
            sa.String(64),
            nullable=False,
            comment=(
                "SHA-256 hex digest of the supporting sentence. "
                "The sentence itself is NOT stored (GDPR)."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_entity_relations_tenant_id",
        "entity_relations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_entity_relations_source_entity_id",
        "entity_relations",
        ["source_entity_id"],
    )
    op.create_index(
        "ix_entity_relations_target_entity_id",
        "entity_relations",
        ["target_entity_id"],
    )
    op.create_index(
        "ix_entity_relations_document_id",
        "entity_relations",
        ["document_id"],
    )
    op.create_index(
        "ix_entity_relations_relation_type",
        "entity_relations",
        ["tenant_id", "relation_type"],
    )


def downgrade() -> None:
    # entity_relations first — it has FKs into medical_entities
    op.drop_index("ix_entity_relations_relation_type", table_name="entity_relations")
    op.drop_index("ix_entity_relations_document_id", table_name="entity_relations")
    op.drop_index("ix_entity_relations_target_entity_id", table_name="entity_relations")
    op.drop_index("ix_entity_relations_source_entity_id", table_name="entity_relations")
    op.drop_index("ix_entity_relations_tenant_id", table_name="entity_relations")
    op.drop_table("entity_relations")

    op.drop_index("ix_medical_entities_icd_code", table_name="medical_entities")
    op.drop_index("ix_medical_entities_document_id", table_name="medical_entities")
    op.drop_index("ix_medical_entities_tenant_type", table_name="medical_entities")
    op.drop_index("ix_medical_entities_tenant_id", table_name="medical_entities")
    op.drop_table("medical_entities")
