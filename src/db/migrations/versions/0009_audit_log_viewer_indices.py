"""Add composite indices to audit_log for efficient viewer queries.

The audit_log table already has basic indices (idx_audit_log_tenant_created,
idx_audit_log_user_id, idx_audit_log_action, idx_audit_log_resource_type).
This migration adds a composite index that makes the admin viewer's common
filter combination (tenant + action prefix + date range) efficient.

Since audit_log is RANGE-partitioned by created_at, we use CREATE INDEX IF NOT EXISTS
and create the index on the parent table so it applies to all partitions.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index for viewer's default filter: tenant + date range (already covered by
    # idx_audit_log_tenant_created but we add resource lookup composite for IDOR filtering)
    op.create_index(
        "ix_audit_log_tenant_resource_created",
        "audit_log",
        ["tenant_id", "resource_type", "resource_id", "created_at"],
        postgresql_where="tenant_id IS NOT NULL",
        if_not_exists=True,
    )
    # Index for action prefix filtering (tenant + action for quick category browsing)
    op.create_index(
        "ix_audit_log_tenant_action_created",
        "audit_log",
        ["tenant_id", "action", "created_at"],
        postgresql_where="tenant_id IS NOT NULL",
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_action_created", table_name="audit_log", if_exists=True)
    op.drop_index("ix_audit_log_tenant_resource_created", table_name="audit_log", if_exists=True)
