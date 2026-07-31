"""Add public collection support to collections table.

Introduces two new columns:

  is_public           — Boolean NOT NULL DEFAULT false
      Marks platform-wide shared collections readable by all tenants.
      Write access is controlled exclusively by managed_by_tenant_id.

  managed_by_tenant_id — UUID nullable FK → tenants.id ON DELETE SET NULL
      The tenant (platform-admin org) that owns and may write this public
      collection. NULL means system-owned; no single tenant may write.

Also adds:
  - ix_collections_is_public — index on is_public for fast public-collection lookups.
  - ix_collections_managed_by_tenant_id — index on managed_by_tenant_id for FK lookups.

Security contract enforced in RetrievalService (not in SQL):
  - Search: (tenant_id == caller) OR (is_public == true AND collection_id IN public_ids)
  - Upsert / Delete: tenant_id == managed_by_tenant_id (non-owners receive PermissionError)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. is_public column — NOT NULL, default false, backfill existing rows.
    op.add_column(
        "collections",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment=(
                "When true, this collection is readable by every tenant. "
                "Write access is restricted to managed_by_tenant_id only."
            ),
        ),
    )

    # 2. managed_by_tenant_id — nullable FK with SET NULL on tenant deletion.
    op.add_column(
        "collections",
        sa.Column(
            "managed_by_tenant_id",
            UUID(as_uuid=True),
            nullable=True,
            comment=(
                "Tenant that owns and may write this public collection. "
                "NULL = system-owned; no tenant may write directly."
            ),
        ),
    )
    op.create_foreign_key(
        "fk_collections_managed_by_tenant_id",
        "collections",
        "tenants",
        ["managed_by_tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. Indexes for performance.
    op.create_index("ix_collections_is_public", "collections", ["is_public"])
    op.create_index(
        "ix_collections_managed_by_tenant_id", "collections", ["managed_by_tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_collections_managed_by_tenant_id", table_name="collections")
    op.drop_index("ix_collections_is_public", table_name="collections")
    op.drop_constraint(
        "fk_collections_managed_by_tenant_id", "collections", type_="foreignkey"
    )
    op.drop_column("collections", "managed_by_tenant_id")
    op.drop_column("collections", "is_public")
