"""Add primary_language column to collections for multi-language query rewriting (TASK-027).

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "primary_language",
            sa.String(10),
            nullable=False,
            server_default="pol",
            comment="ISO 639-3 primary language of documents in this collection.",
        ),
    )


def downgrade() -> None:
    op.drop_column("collections", "primary_language")
