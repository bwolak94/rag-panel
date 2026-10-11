"""Add unique constraints to medical_entities and entity_relations for idempotent upserts.

ADR-023: ON CONFLICT DO NOTHING in node_persist._persist_entities requires a
unique constraint to be present.  Without it, PostgreSQL raises
"there is no unique or exclusion constraint matching the ON CONFLICT specification"
and re-ingesting the same document would insert duplicate rows.

medical_entities natural key: (document_id, canonical_name, entity_type)
entity_relations natural key:  (source_entity_id, target_entity_id, relation_type, document_id)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_medical_entities_natural_key",
        "medical_entities",
        ["document_id", "canonical_name", "entity_type"],
    )
    op.create_unique_constraint(
        "uq_entity_relations_natural_key",
        "entity_relations",
        ["source_entity_id", "target_entity_id", "relation_type", "document_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_entity_relations_natural_key",
        "entity_relations",
        type_="unique",
    )
    op.drop_constraint(
        "uq_medical_entities_natural_key",
        "medical_entities",
        type_="unique",
    )
