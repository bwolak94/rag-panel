"""Add bulk_import_jobs table for ZIP upload and bucket sync tracking.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bulk_import_jobs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("total_files", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("queued_files", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("succeeded_files", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("failed_files", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("skipped_files", sa.Integer, server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'processing'"), nullable=False),
        sa.Column("error_summary", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bulk_import_jobs_tenant_id", "bulk_import_jobs", ["tenant_id"])
    op.create_index("ix_bulk_import_jobs_collection_id", "bulk_import_jobs", ["collection_id"])
    op.create_index("ix_bulk_import_jobs_status", "bulk_import_jobs", ["status"])


def downgrade() -> None:
    op.drop_table("bulk_import_jobs")
