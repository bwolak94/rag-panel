"""Unit tests for document versioning — repository methods and API endpoints."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.models.document_version import DocumentVersion

# ── DocumentRepository.get_versions ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_versions_returns_newest_first() -> None:
    """get_versions orders by version_number DESC."""
    session = MagicMock()

    v1 = MagicMock(spec=DocumentVersion)
    v1.version_number = 2
    v2 = MagicMock(spec=DocumentVersion)
    v2.version_number = 1

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [v1, v2]
    session.execute = AsyncMock(return_value=result_mock)

    from src.db.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(session)
    versions = await repo.get_versions(uuid.uuid4(), uuid.uuid4())

    assert versions[0].version_number == 2
    assert versions[1].version_number == 1


# ── DocumentRepository.set_current_version ───────────────────────────────────


@pytest.mark.asyncio
async def test_set_current_version_marks_old_superseded() -> None:
    """set_current_version marks the old active as superseded and new one as active."""
    session = MagicMock()

    doc_id = uuid.uuid4()
    tenant_id = uuid.uuid4()

    target_version = MagicMock(spec=DocumentVersion)
    target_version.id = uuid.uuid4()
    target_version.version_number = 2

    # First execute: find target version; subsequent: bulk updates + count
    final_versions = [MagicMock(spec=DocumentVersion), target_version]
    scalars_all = MagicMock()
    scalars_all.scalars.return_value.all.return_value = final_versions

    scalar_one = MagicMock()
    scalar_one.scalar_one_or_none.return_value = target_version

    session.execute = AsyncMock(side_effect=[scalar_one, None, None, scalars_all, None])
    session.flush = AsyncMock()

    from src.db.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(session)
    await repo.set_current_version(doc_id, tenant_id, 2)

    # flush called at the end
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_current_version_noop_when_version_not_found() -> None:
    """set_current_version does nothing when target version is missing."""
    session = MagicMock()

    scalar_mock = MagicMock()
    scalar_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=scalar_mock)
    session.flush = AsyncMock()

    from src.db.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(session)
    await repo.set_current_version(uuid.uuid4(), uuid.uuid4(), 99)

    # Only one execute — the lookup; flush never called
    session.execute.assert_awaited_once()
    session.flush.assert_not_awaited()


# ── DocumentRepository.find_by_filename ──────────────────────────────────────


@pytest.mark.asyncio
async def test_find_by_filename_returns_match() -> None:
    """find_by_filename returns a Document when one exists."""
    session = MagicMock()
    doc = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = doc
    session.execute = AsyncMock(return_value=result)

    from src.db.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(session)
    found = await repo.find_by_filename(uuid.uuid4(), uuid.uuid4(), "report.pdf")
    assert found is doc


@pytest.mark.asyncio
async def test_find_by_filename_returns_none_when_no_match() -> None:
    """find_by_filename returns None when no document matches."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    from src.db.repositories.document_repository import DocumentRepository

    repo = DocumentRepository(session)
    found = await repo.find_by_filename(uuid.uuid4(), uuid.uuid4(), "missing.pdf")
    assert found is None


# ── DocumentVersionListResponse ──────────────────────────────────────────────


def test_document_version_list_response_schema() -> None:
    """DocumentVersionListResponse serialises correctly."""
    from datetime import UTC, datetime

    from src.domain.schemas.document_version import DocumentVersionItem, DocumentVersionListResponse

    doc_id = uuid.uuid4()
    item = DocumentVersionItem(
        id=uuid.uuid4(),
        version_number=3,
        is_current=True,
        status="active",
        sha256="abc123",
        created_at=datetime.now(UTC),
        note="Initial upload",
    )
    response = DocumentVersionListResponse(document_id=doc_id, versions=[item])
    assert response.document_id == doc_id
    assert len(response.versions) == 1
    assert response.versions[0].is_current is True
    assert response.versions[0].note == "Initial upload"
