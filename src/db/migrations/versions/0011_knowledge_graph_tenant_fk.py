"""Add FK constraint tenant_id → tenants.id on medical_entities and entity_relations.

Security auditor finding INFO-5: both knowledge-graph tables carry a
`tenant_id` column that was previously a plain (non-FK) UUID.  Adding the FK:

  * guarantees referential integrity at the DB level — orphaned rows whose
    tenant no longer exists are impossible;
  * enables ON DELETE CASCADE so that hard-deleting a tenant row automatically
    removes all its knowledge-graph data (GDPR Art. 17 belt-and-suspenders on
    top of the application-layer DeletionService).

The column already exists and is NOT NULL, so only the constraint is added;
no data migration is required.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # medical_entities.tenant_id → tenants.id
    op.create_foreign_key(
        "fk_medical_entities_tenant_id",
        "medical_entities",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # entity_relations.tenant_id → tenants.id
    op.create_foreign_key(
        "fk_entity_relations_tenant_id",
        "entity_relations",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_entity_relations_tenant_id",
        "entity_relations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_medical_entities_tenant_id",
        "medical_entities",
        type_="foreignkey",
    )
