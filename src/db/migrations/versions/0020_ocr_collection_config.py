"""Add ocr_enabled and ocr_lang columns to collections table.

These columns drive the OCR stage added in TASK-018. ocr_enabled controls whether
node_ocr is allowed to run for a given collection; ocr_lang selects the Tesseract
language pack(s) to use (e.g. "pol+eng").

Both columns are also stored in chunk_config JSON for runtime reads by node_ocr,
but the dedicated columns allow efficient SQL queries and schema documentation.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "ocr_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
            comment="When True, node_ocr is permitted to run for this collection.",
        ),
    )
    op.add_column(
        "collections",
        sa.Column(
            "ocr_lang",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pol+eng'"),
            comment="Tesseract language string passed to pytesseract (e.g. 'pol+eng').",
        ),
    )


def downgrade() -> None:
    op.drop_column("collections", "ocr_lang")
    op.drop_column("collections", "ocr_enabled")
