"""Add ab_test_results table for prompt AB-testing shadow mode.

Records per-request comparison metrics between two prompt versions.
Answer text, question text, and chunk content are NEVER stored here (GDPR).
Only non-content observability metrics are persisted:
  - control_length / shadow_length  — character counts of the respective answers
  - latency_control_ms / latency_shadow_ms — wall-clock LLM call times
  - experiment_id / control_version / shadow_version — experiment metadata
  - tenant_id (denormalised, NOT a FK — survives tenant deletion)
  - pipeline_id / conversation_id  — nullable FKs, SET NULL on delete

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ab_test_results",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            nullable=False,
            comment=(
                "Tenant that owns this result. Denormalised — not a FK "
                "so rows survive tenant-level operations."
            ),
        ),
        sa.Column(
            "pipeline_id",
            UUID(as_uuid=True),
            sa.ForeignKey("rag_pipelines.id", ondelete="SET NULL"),
            nullable=True,
            comment="Pipeline the request was routed through. NULL when pipeline has been deleted.",
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
            comment="Conversation the request belongs to. NULL when conversation has been deleted.",
        ),
        sa.Column(
            "experiment_id",
            sa.String(255),
            nullable=False,
            comment="Logical experiment identifier from prompt_config.ab_test.experiment_id.",
        ),
        sa.Column(
            "control_version",
            sa.String(64),
            nullable=False,
            comment="Prompt version used for the control (main) generation, e.g. 'v1'.",
        ),
        sa.Column(
            "shadow_version",
            sa.String(64),
            nullable=False,
            comment="Prompt version used for the shadow generation, e.g. 'v2'.",
        ),
        sa.Column(
            "control_length",
            sa.Integer(),
            nullable=False,
            comment="Character length of the control answer. Never the answer text itself.",
        ),
        sa.Column(
            "shadow_length",
            sa.Integer(),
            nullable=False,
            comment="Character length of the shadow answer. Never the answer text itself.",
        ),
        sa.Column(
            "latency_control_ms",
            sa.Float(),
            nullable=False,
            comment="Wall-clock time in milliseconds for the control LLM call.",
        ),
        sa.Column(
            "latency_shadow_ms",
            sa.Float(),
            nullable=False,
            comment="Wall-clock time in milliseconds for the shadow LLM call.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    # Indexes for the primary analytical query patterns.
    op.create_index("ix_ab_test_results_tenant_id", "ab_test_results", ["tenant_id"])
    op.create_index("ix_ab_test_results_experiment_id", "ab_test_results", ["experiment_id"])
    op.create_index("ix_ab_test_results_pipeline_id", "ab_test_results", ["pipeline_id"])
    op.create_index("ix_ab_test_results_conversation_id", "ab_test_results", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_ab_test_results_conversation_id", table_name="ab_test_results")
    op.drop_index("ix_ab_test_results_pipeline_id", table_name="ab_test_results")
    op.drop_index("ix_ab_test_results_experiment_id", table_name="ab_test_results")
    op.drop_index("ix_ab_test_results_tenant_id", table_name="ab_test_results")
    op.drop_table("ab_test_results")
