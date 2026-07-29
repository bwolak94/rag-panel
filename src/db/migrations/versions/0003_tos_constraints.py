"""Add unique constraint on tos_acceptances(tenant_id, tos_version_id) and
CheckConstraint on tos_versions.status.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Prevent duplicate acceptances — closes race condition in TosService.accept_tos()
    op.create_unique_constraint(
        "uq_tos_acceptances_tenant_version",
        "tos_acceptances",
        ["tenant_id", "tos_version_id"],
    )

    # Enforce allowed status values at the DB level
    op.create_check_constraint(
        "ck_tos_versions_status",
        "tos_versions",
        "status IN ('draft', 'active', 'superseded')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tos_versions_status", "tos_versions", type_="check")
    op.drop_constraint(
        "uq_tos_acceptances_tenant_version", "tos_acceptances", type_="unique"
    )
