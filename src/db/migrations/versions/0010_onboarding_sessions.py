"""Add onboarding_sessions table for tenant self-service onboarding wizard.

Tracks the stateful multi-step onboarding flow initiated by a platform-admin.
Each row represents one onboarding attempt for a new tenant. Steps 1-5 are
stored as JSON in steps_completed; draft_config accumulates config across steps.

Sessions expire after 7 days (enforced by application, not DB-level TTL).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
            comment="Set when tenant is created in step 1. NULL before step 1 completes.",
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="in_progress",
            comment="in_progress | completed | abandoned",
        ),
        sa.Column(
            "current_step",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Last successfully completed step number (1-5).",
        ),
        sa.Column(
            "steps_completed",
            JSONB,
            nullable=False,
            server_default='{}',
            comment='Map of step number → bool, e.g. {"1": true, "2": true}.',
        ),
        sa.Column(
            "draft_config",
            JSONB,
            nullable=False,
            server_default='{}',
            comment="Accumulated configuration across steps. Not sensitive — no secrets.",
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            comment="Platform-admin user who started the onboarding.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Sessions are abandoned automatically after this time (7 days from creation).",
        ),
    )
    op.create_index("ix_onboarding_sessions_tenant_id", "onboarding_sessions", ["tenant_id"])
    op.create_index("ix_onboarding_sessions_status", "onboarding_sessions", ["status"])
    op.create_index("ix_onboarding_sessions_expires_at", "onboarding_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_onboarding_sessions_expires_at", table_name="onboarding_sessions")
    op.drop_index("ix_onboarding_sessions_status", table_name="onboarding_sessions")
    op.drop_index("ix_onboarding_sessions_tenant_id", table_name="onboarding_sessions")
    op.drop_table("onboarding_sessions")
