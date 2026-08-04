"""Create onboarding_sessions table for tenant self-service wizard.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column("current_step", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "steps_completed",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "draft_config",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_onboarding_sessions_tenant_id", "onboarding_sessions", ["tenant_id"])
    op.create_index("ix_onboarding_sessions_status", "onboarding_sessions", ["status"])
    op.create_index("ix_onboarding_sessions_expires_at", "onboarding_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_onboarding_sessions_expires_at", table_name="onboarding_sessions")
    op.drop_index("ix_onboarding_sessions_status", table_name="onboarding_sessions")
    op.drop_index("ix_onboarding_sessions_tenant_id", table_name="onboarding_sessions")
    op.drop_table("onboarding_sessions")
