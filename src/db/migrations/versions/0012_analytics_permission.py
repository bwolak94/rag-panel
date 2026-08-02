"""Seed admin:analytics permission.

Adds the 'admin:analytics' permission code to the permissions table so that
roles granting access to the usage analytics API can be created.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO permissions (code, description) "
            "VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(
            code="admin:analytics",
            description="View per-tenant usage analytics dashboards",
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM permissions WHERE code = :code").bindparams(
            code="admin:analytics"
        )
    )
