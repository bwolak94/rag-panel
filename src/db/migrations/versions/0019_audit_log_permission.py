"""Seed admin:audit_log permission and assign to Admin role.

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Insert the permission code if it doesn't already exist
    op.execute(
        sa.text(
            "INSERT INTO permissions (code, description) "
            "VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(
            code="admin:audit_log",
            description="Browse and export the tenant audit log",
        )
    )

    # Grant admin:audit_log to every tenant's 'admin' system role.
    # Uses a sub-select to stay data-driven — no hardcoded IDs.
    op.execute(
        sa.text(
            """
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id
            FROM roles r
            CROSS JOIN permissions p
            WHERE r.name = 'admin'
              AND r.is_system = TRUE
              AND p.code = 'admin:audit_log'
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id = (
                SELECT id FROM permissions WHERE code = 'admin:audit_log'
            )
            """
        )
    )
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(
            code="admin:audit_log"
        )
    )
