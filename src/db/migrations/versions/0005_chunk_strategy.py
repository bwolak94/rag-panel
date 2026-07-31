"""Document adaptive chunking strategy keys in chunk_config column comment.

No schema change is required — the chunk_config JSONB column already exists.
This migration updates the column comment to record the newly supported keys:

  "strategy": "recursive" | "section_aware" | "sentence" | "row"
  "strategy_by_category": {"<category>": "<strategy>", ...}
  "sentences_per_chunk": int
  "overlap_sentences": int
  "rows_per_chunk": int

See src/graphs/ingest_graph/chunking.py for full implementation.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_COMMENT = (
    "Chunking configuration for this collection. "
    "Supported keys: "
    "strategy (recursive|section_aware|sentence|row), "
    "strategy_by_category ({category: strategy}), "
    "chunk_size (int, default 512), "
    "overlap (int, default 64), "
    "min_chunk_size (int, default 64), "
    "sentences_per_chunk (int, default 4), "
    "overlap_sentences (int, default 1), "
    "rows_per_chunk (int, default 10), "
    "document_type_overrides ({doc_type: {chunk_size, overlap}})."
)


def upgrade() -> None:
    op.execute(
        f"COMMENT ON COLUMN collections.chunk_config IS $${_COMMENT}$$"
    )


def downgrade() -> None:
    op.execute("COMMENT ON COLUMN collections.chunk_config IS NULL")
