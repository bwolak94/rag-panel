"""Add search_config JSONB column to collections table.

search_config controls the retrieval strategy per collection:
  {"search_mode": "dense" | "hybrid", "top_k": int, "score_threshold": float}

HYBRID enables BM25 + dense vector fusion via Reciprocal Rank Fusion.
Column is nullable; NULL means fall back to application defaults (dense, top_k=8).

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("search_config", JSONB(), nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("collections", "search_config")
