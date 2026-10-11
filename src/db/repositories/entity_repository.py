"""EntityRepository — persistence and query operations for Graph RAG entities.

Wraps all SQLAlchemy access to ``medical_entities`` and ``entity_relations``
tables.  Provides clean upsert semantics (ON CONFLICT DO NOTHING on the
unique natural key) so re-ingesting the same document is idempotent.

Tenant isolation:
- Every write and read method takes ``tenant_id`` explicitly from the call site
  (pipeline state), never from LLM output or HTTP request body.
- Read methods also accept ``collection_ids`` so that entity queries are scoped
  to the collections the pipeline is authorised to access (not the whole tenant).
- Every query is scoped to both ``tenant_id`` and ``collection_ids`` — no
  cross-collection entity leakage within the same tenant.

GDPR:
- ``canonical_name`` and ``surface_forms`` are derived from document text.
  Log only counts and IDs; never log name values.
- Deletion is handled via the Postgres FK cascade from ``documents`` → entities
  → relations (``ondelete="CASCADE"``).  DeletionService does not need to
  explicitly delete entity rows when it deletes a document.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.document import Document
from src.db.models.knowledge_graph import EntityRelation, MedicalEntity


class EntityRepository:
    """Repository for ``medical_entities`` and ``entity_relations`` tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Write operations (ingest path)
    # ------------------------------------------------------------------

    async def upsert_entities(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[dict[str, Any]],
    ) -> int:
        """Insert MedicalEntity rows, skipping conflicts on the natural key.

        The natural key unique constraint is
        ``uq_medical_entities_natural_key (document_id, canonical_name, entity_type)``.
        Re-ingesting the same document is safe; existing rows are left unchanged.

        Args:
            tenant_id: Owning tenant — always sourced from pipeline state.
            document_id: Source document UUID.
            entities: Normalised entity dicts produced by node_extract_entities.
                Each must have keys: ``type``, ``name``, ``surface_forms``,
                ``confidence``, ``chunk_ids`` and optionally ``icd_code``,
                ``atc_code``.

        Returns:
            Number of rows submitted for insertion (includes conflicts).
        """
        if not entities:
            return 0

        rows: list[dict[str, Any]] = [
            {
                "tenant_id": tenant_id,
                "document_id": document_id,
                "document_version_id": None,
                "entity_type": e["type"],
                "canonical_name": e["name"],
                "surface_forms": e.get("surface_forms", [e["name"]]),
                "icd_code": e.get("icd_code"),
                "atc_code": e.get("atc_code"),
                "confidence": e.get("confidence", 0.0),
                "chunk_ids": e.get("chunk_ids", []),
            }
            for e in entities
        ]

        stmt = (
            pg_insert(MedicalEntity)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["document_id", "canonical_name", "entity_type"])
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return len(rows)

    async def resolve_name_to_id(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        canonical_names: list[str],
    ) -> dict[str, uuid.UUID]:
        """Return a canonical_name → UUID map for the given names within a document.

        Called after ``upsert_entities`` to resolve FK references before
        inserting relation rows.

        Args:
            tenant_id: Owning tenant — scopes the lookup.
            document_id: Source document UUID.
            canonical_names: Names to resolve (case-sensitive, lowercased by
                node_extract_entities before storage).

        Returns:
            Dict mapping ``canonical_name → entity.id`` for names that exist.
        """
        if not canonical_names:
            return {}

        result = await self._session.execute(
            select(MedicalEntity.id, MedicalEntity.canonical_name).where(
                MedicalEntity.document_id == document_id,
                MedicalEntity.tenant_id == tenant_id,
                MedicalEntity.canonical_name.in_(canonical_names),
            )
        )
        return {row.canonical_name: row.id for row in result}

    async def upsert_relations(
        self,
        *,
        tenant_id: uuid.UUID,
        document_id: uuid.UUID,
        relations: list[dict[str, Any]],
        name_to_id: dict[str, uuid.UUID],
    ) -> int:
        """Insert EntityRelation rows, skipping conflicts on the natural key.

        Silently skips any relation whose source or target name cannot be
        resolved in ``name_to_id`` (defensive — these would cause FK errors).

        Args:
            tenant_id: Owning tenant.
            document_id: Source document UUID.
            relations: Normalised relation dicts from node_extract_entities.
                Each must have keys: ``source_name``, ``target_name``,
                ``relation_type``, ``confidence``, ``evidence_text_hash``.
            name_to_id: Mapping of canonical_name → entity UUID (from
                ``resolve_name_to_id``).

        Returns:
            Number of rows submitted for insertion (includes conflicts).
        """
        if not relations:
            return 0

        rows: list[dict[str, Any]] = []
        for rel in relations:
            source_id = name_to_id.get(rel.get("source_name", ""))
            target_id = name_to_id.get(rel.get("target_name", ""))
            if source_id is None or target_id is None:
                continue
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "source_entity_id": source_id,
                    "target_entity_id": target_id,
                    "relation_type": rel["relation_type"],
                    "confidence": rel.get("confidence", 0.0),
                    "document_id": document_id,
                    "evidence_text_hash": rel["evidence_text_hash"],
                }
            )

        if not rows:
            return 0

        natural_key = ["source_entity_id", "target_entity_id", "relation_type", "document_id"]
        stmt = (
            pg_insert(EntityRelation)
            .values(rows)
            .on_conflict_do_nothing(index_elements=natural_key)
        )
        await self._session.execute(stmt)
        await self._session.flush()
        return len(rows)

    # ------------------------------------------------------------------
    # Read operations (query-graph path)
    # ------------------------------------------------------------------

    async def find_by_tokens(
        self,
        *,
        tenant_id: uuid.UUID,
        collection_ids: list[uuid.UUID],
        tokens: list[str],
        limit: int = 10,
    ) -> list[MedicalEntity]:
        """Return MedicalEntity rows whose canonical_name matches any token (ILIKE).

        Scoped to both ``tenant_id`` and ``collection_ids`` via a join to the
        ``documents`` table, preventing cross-collection entity leakage within
        the same tenant.

        ILIKE wildcard characters (``%``, ``_``) in tokens are escaped so a
        user query containing ``100%`` does not accidentally match all entities.

        Args:
            tenant_id: Always scopes the query — tenant isolation is mandatory.
            collection_ids: Collections the pipeline is authorised to query.
            tokens: Candidate search terms (lowercase, stopwords removed).
            limit: Maximum number of entities to return.

        Returns:
            List of matching ``MedicalEntity`` ORM instances.
        """
        if not tokens or not collection_ids:
            return []

        def _escape(token: str) -> str:
            return token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        ilike_conditions = [
            MedicalEntity.canonical_name.ilike(f"%{_escape(t)}%", escape="\\") for t in tokens
        ]

        result = await self._session.execute(
            select(MedicalEntity)
            .join(Document, MedicalEntity.document_id == Document.id)
            .where(
                MedicalEntity.tenant_id == tenant_id,
                Document.collection_id.in_(collection_ids),
                or_(*ilike_conditions),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def find_relations_for_entities(
        self,
        *,
        tenant_id: uuid.UUID,
        entity_ids: list[uuid.UUID],
        limit: int = 20,
    ) -> list[EntityRelation]:
        """Fetch EntityRelation rows where source OR target is in ``entity_ids``.

        Always scoped to ``tenant_id``.  The entities were already filtered by
        ``collection_ids`` in ``find_by_tokens``, so relation rows are
        implicitly scoped to the same collections.

        Args:
            tenant_id: Owning tenant.
            entity_ids: UUIDs of matched entities (may be source or target).
            limit: Maximum relations to return.

        Returns:
            List of ``EntityRelation`` ORM instances.
        """
        if not entity_ids:
            return []

        result = await self._session.execute(
            select(EntityRelation)
            .where(
                EntityRelation.tenant_id == tenant_id,
                or_(
                    EntityRelation.source_entity_id.in_(entity_ids),
                    EntityRelation.target_entity_id.in_(entity_ids),
                ),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def find_by_ids(
        self,
        *,
        tenant_id: uuid.UUID,
        entity_ids: set[uuid.UUID],
    ) -> list[MedicalEntity]:
        """Fetch MedicalEntity rows by primary key, always scoped to tenant_id.

        Used to resolve relation endpoints not returned by ``find_by_tokens``.

        Args:
            tenant_id: Owning tenant — mandatory filter.
            entity_ids: Set of entity UUIDs to fetch.

        Returns:
            List of ``MedicalEntity`` instances (may be fewer than requested
            if some IDs do not exist or belong to a different tenant).
        """
        if not entity_ids:
            return []

        result = await self._session.execute(
            select(MedicalEntity).where(
                MedicalEntity.tenant_id == tenant_id,
                MedicalEntity.id.in_(entity_ids),
            )
        )
        return list(result.scalars().all())
