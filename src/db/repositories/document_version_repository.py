"""Repository for DocumentVersion CRUD operations.

All public methods accept `tenant_id` as their first argument and include it
in every WHERE clause — no cross-tenant data is ever returned.

The repository works directly against ORM models and returns ORM instances;
mapping to domain models is left to the calling service layer (not yet
implemented — see docs/data-model.md §document_versions).

Transaction boundary: the caller (router or service) owns the session and
must call `session.commit()`.  This repository only calls `session.flush()`
when it needs a freshly-generated PK before the transaction ends.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.document import Document
from src.db.models.document_version import DocumentVersion


class DocumentVersionRepository:
    """Data-access layer for document versioning.

    Args:
        session: An open async SQLAlchemy session.  The caller is responsible
            for committing or rolling back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_active_version(
        self, document_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> DocumentVersion | None:
        """Return the currently active version of a document, or None.

        Args:
            document_id: The document whose active version is requested.
            tenant_id: Tenant scope — prevents cross-tenant access.

        Returns:
            The single DocumentVersion with status='active', or None if the
            document has no active version yet (e.g. still in draft/indexing).
        """
        q = select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.status == "active",
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def get_version_by_number(
        self,
        document_id: uuid.UUID,
        version_number: int,
        tenant_id: uuid.UUID,
    ) -> DocumentVersion | None:
        """Return a specific numbered version of a document, or None.

        Args:
            document_id: The parent document.
            version_number: 1-based version number.
            tenant_id: Tenant scope.

        Returns:
            The matching DocumentVersion, or None if it does not exist.
        """
        q = select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_number == version_number,
            DocumentVersion.tenant_id == tenant_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def get_by_id(
        self, version_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> DocumentVersion | None:
        """Return a single version by its PK, scoped to tenant.

        Args:
            version_id: PK of the document_versions row.
            tenant_id: Tenant scope.

        Returns:
            The DocumentVersion, or None if not found or belongs to another tenant.
        """
        q = select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.tenant_id == tenant_id,
        )
        return (await self._session.execute(q)).scalar_one_or_none()

    async def list_versions(
        self,
        document_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> list[DocumentVersion]:
        """Return all versions of a document, newest first.

        Args:
            document_id: The parent document.
            tenant_id: Tenant scope.
            offset: Pagination offset.
            limit: Maximum number of rows to return.

        Returns:
            List of DocumentVersion ordered by version_number DESC.
        """
        q = (
            select(DocumentVersion)
            .where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.tenant_id == tenant_id,
            )
            .order_by(DocumentVersion.version_number.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self._session.execute(q)).scalars().all())

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def create_new_version(
        self,
        document_id: uuid.UUID,
        tenant_id: uuid.UUID,
        sha256: str,
        *,
        ingestion_job_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        activate: bool = True,
    ) -> DocumentVersion:
        """Create the next version of a document and optionally activate it.

        The method:
        1. Determines the next version_number by inspecting existing rows
           (MAX + 1, starts at 1 for the first version).
        2. If `activate=True`, marks the current active version as 'superseded'
           (sets valid_to=now, status='superseded', superseded_by_version_id=new.id)
           and updates `documents.current_version_id` and `documents.version_count`.
        3. Inserts the new DocumentVersion row with status='active' (or 'draft'
           if activate=False).
        4. Flushes so callers can read the generated PK within the same transaction.

        Both the FK from document_versions → documents and the FK from
        documents → document_versions are DEFERRABLE INITIALLY DEFERRED, so
        all of the above safely fits within a single transaction.

        Args:
            document_id: Parent document UUID.
            tenant_id: Tenant scope.
            sha256: Content hash of the new version's source file.
            ingestion_job_id: Optional FK to the IngestionJob that produced
                this version.
            metadata: Arbitrary JSONB metadata (e.g. change_summary, author).
                May contain PII — never log.
            activate: If True (default), the new version becomes 'active' and
                the previous active version is superseded.

        Returns:
            The newly created DocumentVersion (flushed, PK available).

        Raises:
            ValueError: If the document does not belong to the given tenant.
        """
        # Verify tenant ownership before mutating.
        doc_q = select(Document).where(
            Document.id == document_id,
            Document.tenant_id == tenant_id,
        )
        document = (await self._session.execute(doc_q)).scalar_one_or_none()
        if document is None:
            raise ValueError(f"Document {document_id} not found for tenant {tenant_id}")

        # Determine next version number.
        from sqlalchemy import func

        max_q = select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.tenant_id == tenant_id,
        )
        current_max: int | None = (await self._session.execute(max_q)).scalar_one_or_none()
        next_version_number = 1 if current_max is None else current_max + 1

        now = datetime.now(tz=UTC)

        new_version = DocumentVersion(
            document_id=document_id,
            tenant_id=tenant_id,
            version_number=next_version_number,
            sha256=sha256,
            status="active" if activate else "draft",
            valid_from=now,
            valid_to=None,
            ingestion_job_id=ingestion_job_id,
            metadata_json=metadata,
        )
        self._session.add(new_version)
        # Flush early to get new_version.id — needed for the superseded_by FK
        # and for updating documents.current_version_id.
        await self._session.flush()

        if activate:
            # Supersede the currently active version (if any).
            await self._session.execute(
                update(DocumentVersion)
                .where(
                    DocumentVersion.document_id == document_id,
                    DocumentVersion.tenant_id == tenant_id,
                    DocumentVersion.status == "active",
                    DocumentVersion.id != new_version.id,
                )
                .values(
                    status="superseded",
                    valid_to=now,
                    superseded_by_version_id=new_version.id,
                )
            )

            # Update the denormalised pointer and counter on the parent document.
            await self._session.execute(
                update(Document)
                .where(
                    Document.id == document_id,
                    Document.tenant_id == tenant_id,
                )
                .values(
                    current_version_id=new_version.id,
                    version_count=next_version_number,
                )
            )
            await self._session.flush()

        return new_version

    async def activate_version(
        self,
        version_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> DocumentVersion:
        """Promote a draft version to active, superseding the current active one.

        Args:
            version_id: The draft version to activate.
            tenant_id: Tenant scope.

        Returns:
            The updated DocumentVersion with status='active'.

        Raises:
            ValueError: If the version is not found, belongs to another tenant,
                or is not in 'draft' status.
        """
        version = await self.get_by_id(version_id, tenant_id)
        if version is None:
            raise ValueError(f"DocumentVersion {version_id} not found for tenant {tenant_id}")
        if version.status != "draft":
            raise ValueError(
                f"Only 'draft' versions can be activated; current status is '{version.status}'"
            )

        now = datetime.now(tz=UTC)

        # Supersede current active version.
        await self._session.execute(
            update(DocumentVersion)
            .where(
                DocumentVersion.document_id == version.document_id,
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.status == "active",
            )
            .values(
                status="superseded",
                valid_to=now,
                superseded_by_version_id=version_id,
            )
        )

        # Activate the target version.
        await self._session.execute(
            update(DocumentVersion)
            .where(
                DocumentVersion.id == version_id,
                DocumentVersion.tenant_id == tenant_id,
            )
            .values(status="active", valid_from=now, valid_to=None)
        )

        # Update denormalised pointer on parent document.
        await self._session.execute(
            update(Document)
            .where(
                Document.id == version.document_id,
                Document.tenant_id == tenant_id,
            )
            .values(current_version_id=version_id)
        )

        await self._session.flush()

        # Re-fetch to return a consistent state.
        refreshed = await self.get_by_id(version_id, tenant_id)
        if refreshed is None:
            # Should never happen: we just flushed the update above and the
            # FK guarantees the row exists.  Raise explicitly so mypy is
            # satisfied and the caller gets a clear traceback rather than an
            # AttributeError on None.
            raise RuntimeError(
                f"DocumentVersion {version_id} disappeared immediately after activation — "
                "this indicates a transaction isolation anomaly or a FK violation."
            )
        return refreshed
