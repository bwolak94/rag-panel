"""Add version_group_id, version_number, is_current to documents for ADR-024.

ADR-024 (Document Versioning): tracks that "Procedure_v2.pdf" supersedes
"Procedure_v1.pdf".  Retrieval returns only current versions by default;
historical versions can be queried via include_historical_versions=True.

Columns added to ``documents``:
- version_group_id UUID  — shared by all versions of the same logical document.
  Populated by a DO UPDATE so existing rows self-assign their own ``id`` as the
  group, making them first-generation unversioned documents.
- version_number INT NOT NULL DEFAULT 1 — sequential within the version group.
- is_current BOOL NOT NULL DEFAULT TRUE — True for the latest version.

Unique constraint: (tenant_id, version_group_id, version_number) prevents
duplicate version numbers within a group per tenant.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add columns (nullable first so backfill can run before NOT NULL is enforced).
    op.add_column(
        "documents",
        sa.Column("version_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "version_number",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "documents",
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # 2. Backfill: existing rows become their own version group (version 1, is_current=true).
    op.execute(sa.text("UPDATE documents SET version_group_id = id WHERE version_group_id IS NULL"))

    # 3. Now safe to enforce NOT NULL.
    op.alter_column("documents", "version_group_id", nullable=False)

    # 4. Indexes for fast filtering.
    op.create_index("ix_documents_version_group_id", "documents", ["version_group_id"])
    op.create_index("ix_documents_is_current", "documents", ["is_current"])

    # 5. Unique constraint — prevents duplicate version numbers per group per tenant.
    op.create_unique_constraint(
        "uq_documents_version_group_number",
        "documents",
        ["tenant_id", "version_group_id", "version_number"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_documents_version_group_number", "documents", type_="unique")
    op.drop_index("ix_documents_is_current", table_name="documents")
    op.drop_index("ix_documents_version_group_id", table_name="documents")
    op.drop_column("documents", "is_current")
    op.drop_column("documents", "version_number")
    op.drop_column("documents", "version_group_id")
