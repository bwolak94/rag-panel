"""Add dedup_embedding column to documents for semantic near-duplicate detection.

Adds a nullable JSONB column that stores the mean-pooled document-level embedding
produced by node_semantic_dedup. This enables cosine-similarity comparison against
existing documents in the same collection during ingest.

Also adds columns to track the dedup result for admin audit:
  dedup_similar_doc_id  — UUID of the most similar existing document (if any)
  dedup_similarity      — cosine similarity score (0.0–1.0)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mean-pooled embedding from chunk embeddings — nullable (only set when semantic_dedup_enabled)
    op.add_column(
        "documents",
        sa.Column(
            "dedup_embedding",
            JSONB,
            nullable=True,
            comment=(
                "Mean-pooled document-level embedding for semantic deduplication. "
                "GDPR: never log this value — it encodes document content. "
                "NULL when semantic_dedup_enabled=False for the collection."
            ),
        ),
    )
    # UUID of the most similar document found during dedup check (for admin audit)
    op.add_column(
        "documents",
        sa.Column(
            "dedup_similar_doc_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="UUID of the most similar existing document found by semantic dedup. NULL if unique.",
        ),
    )
    # Cosine similarity score (0.0 – 1.0) — highest found across all documents in the collection
    op.add_column(
        "documents",
        sa.Column(
            "dedup_similarity",
            sa.Float(),
            nullable=True,
            comment=(
                "Highest cosine similarity score found during semantic dedup (0.0–1.0). "
                "NULL if unique or if dedup was not run."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "dedup_similarity")
    op.drop_column("documents", "dedup_similar_doc_id")
    op.drop_column("documents", "dedup_embedding")
