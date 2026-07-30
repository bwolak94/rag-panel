"""Document versioning schema (VersionRAG).

Adds the `document_versions` table to track immutable content snapshots of
medical protocols and other documents. Extends `documents` with
`current_version_id` (FK → document_versions) and `version_count`.
Extends `chunks_registry` with `document_version_id` so each chunk is tied
to the exact version it was extracted from.

Circular FK: documents.current_version_id ↔ document_versions.document_id.
Strategy:
  1. Create `document_versions` with document_id FK DEFERRABLE INITIALLY DEFERRED.
  2. Add `current_version_id` + `version_count` columns to `documents` without
     the FK constraint (nullable, no constraint yet).
  3. Add the FK `fk_documents_current_version_id` via ADD CONSTRAINT … DEFERRABLE
     INITIALLY DEFERRED so both tables now reference each other, but the
     constraint is only checked at COMMIT — a single transaction can insert
     both rows safely.
  4. Add `document_version_id` to `chunks_registry`.

downgrade() reverses all of the above in the exact reverse order.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. document_versions table
    # ------------------------------------------------------------------
    op.create_table(
        "document_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # DEFERRABLE FK — resolved at commit, not at statement time.
        # This is required because documents.current_version_id will point
        # back to this table, creating a circular dependency.
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "documents.id",
                ondelete="CASCADE",
                deferrable=True,
                initially="DEFERRED",
                name="fk_document_versions_document_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_document_versions_tenant_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "ingestion_jobs.id",
                ondelete="SET NULL",
                name="fk_document_versions_ingestion_job_id",
            ),
            nullable=True,
        ),
        # Self-referential: points to the version that superseded this one.
        sa.Column(
            "superseded_by_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Business constraints
        sa.UniqueConstraint(
            "document_id",
            "version_number",
            name="uq_document_versions_doc_version",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'draft')",
            name="ck_document_versions_status",
        ),
    )

    # Self-referential FK added after the table exists.
    op.create_foreign_key(
        "fk_document_versions_superseded_by",
        "document_versions",
        "document_versions",
        ["superseded_by_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Indexes
    op.create_index(
        "ix_document_versions_tenant_id",
        "document_versions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_document_versions_tenant_document",
        "document_versions",
        ["tenant_id", "document_id"],
    )
    # Partial-like composite index: fast "give me the active version for doc X"
    op.create_index(
        "ix_document_versions_document_status",
        "document_versions",
        ["document_id", "status"],
    )

    # updated_at trigger (same pattern as other tables in migration 0001)
    op.execute(
        """
        CREATE TRIGGER trg_document_versions_updated_at
        BEFORE UPDATE ON document_versions
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column()
        """
    )

    # ------------------------------------------------------------------
    # 2. Extend documents table
    # ------------------------------------------------------------------
    op.add_column(
        "documents",
        sa.Column(
            "current_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "version_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # Deferred FK from documents → document_versions (the other half of the circle).
    op.create_foreign_key(
        "fk_documents_current_version_id",
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_index(
        "ix_documents_current_version_id",
        "documents",
        ["current_version_id"],
    )

    # ------------------------------------------------------------------
    # 3. Extend chunks_registry table
    # ------------------------------------------------------------------
    op.add_column(
        "chunks_registry",
        sa.Column(
            "document_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "document_versions.id",
                ondelete="SET NULL",
                name="fk_chunks_registry_document_version_id",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_chunks_registry_document_version_id",
        "chunks_registry",
        ["document_version_id"],
    )


def downgrade() -> None:
    # Reverse order: chunks_registry → documents columns → document_versions table

    # ------------------------------------------------------------------
    # 3. Remove chunks_registry extension
    # ------------------------------------------------------------------
    op.drop_index("ix_chunks_registry_document_version_id", table_name="chunks_registry")
    op.drop_constraint(
        "fk_chunks_registry_document_version_id", "chunks_registry", type_="foreignkey"
    )
    op.drop_column("chunks_registry", "document_version_id")

    # ------------------------------------------------------------------
    # 2. Remove documents extension
    # ------------------------------------------------------------------
    op.drop_index("ix_documents_current_version_id", table_name="documents")
    op.drop_constraint("fk_documents_current_version_id", "documents", type_="foreignkey")
    op.drop_column("documents", "version_count")
    op.drop_column("documents", "current_version_id")

    # ------------------------------------------------------------------
    # 1. Drop document_versions table
    # ------------------------------------------------------------------
    op.execute("DROP TRIGGER IF EXISTS trg_document_versions_updated_at ON document_versions")
    op.drop_index("ix_document_versions_document_status", table_name="document_versions")
    op.drop_index("ix_document_versions_tenant_document", table_name="document_versions")
    op.drop_index("ix_document_versions_tenant_id", table_name="document_versions")
    op.drop_constraint(
        "fk_document_versions_superseded_by", "document_versions", type_="foreignkey"
    )
    op.drop_table("document_versions")
