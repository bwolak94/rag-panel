"""Add composite indices to audit_log for efficient filtering.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index for time-range queries (most common pattern: list by tenant + time)
    op.create_index(
        "ix_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", "created_at"],
        postgresql_ops={"created_at": "DESC"},
        if_not_exists=True,
    )
    # Index for action-based filtering
    op.create_index(
        "ix_audit_log_tenant_action",
        "audit_log",
        ["tenant_id", "action"],
        if_not_exists=True,
    )
    # Index for resource-based filtering
    op.create_index(
        "ix_audit_log_tenant_resource",
        "audit_log",
        ["tenant_id", "resource_type", "resource_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_tenant_resource", table_name="audit_log", if_exists=True)
    op.drop_index("ix_audit_log_tenant_action", table_name="audit_log", if_exists=True)
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log", if_exists=True)
