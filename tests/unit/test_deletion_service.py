"""Unit tests for DeletionService — GDPR Art. 17 cascading deletion.

All external dependencies (Qdrant, MinIO, DB) are mocked.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.exceptions import NotFoundError
from src.domain.auth import UserContext

TENANT_ID = uuid.uuid4()
OTHER_TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_ctx(tenant_id: uuid.UUID = TENANT_ID) -> UserContext:
    return UserContext(
        user_id=USER_ID,
        keycloak_sub="kc-user",
        email="admin@example.com",
        display_name="Admin",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=frozenset({"documents:manage", "documents:approve"}),
        allowed_collection_ids=frozenset({COLLECTION_ID}),
        writable_collection_ids=frozenset({COLLECTION_ID}),
    )


def _make_doc(tenant_id: uuid.UUID = TENANT_ID) -> MagicMock:
    doc = MagicMock()
    doc.id = DOCUMENT_ID
    doc.tenant_id = tenant_id
    doc.collection_id = COLLECTION_ID
    doc.minio_key = f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/file.pdf"
    return doc


def _make_collection() -> MagicMock:
    coll = MagicMock()
    coll.id = COLLECTION_ID
    coll.embedding_model_id = uuid.uuid4()
    return coll


def _make_model() -> MagicMock:
    model = MagicMock()
    model.name = "BGE-M3"
    return model


def _make_tenant() -> MagicMock:
    tenant = MagicMock()
    tenant.id = TENANT_ID
    tenant.slug = "test-clinic"
    return tenant


def _make_session(
    doc: MagicMock | None,
    collection: MagicMock | None = None,
    model: MagicMock | None = None,
    tenant: MagicMock | None = None,
    conversation: MagicMock | None = None,
) -> MagicMock:
    """Build a mock AsyncSession that returns the given objects in sequence."""
    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    # Each scalar_one_or_none() call returns the next value
    scalars: list[MagicMock | None] = [doc, collection, model]
    call_count: list[int] = [0]

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        idx = call_count[0]
        call_count[0] += 1
        values = scalars + [tenant, conversation]
        val = values[idx] if idx < len(values) else None
        result.scalar_one_or_none = MagicMock(return_value=val)
        return result

    session.execute = _execute

    return session


# ── delete_document ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_document_success() -> None:
    """Happy path: all external calls succeed."""
    from src.domain.deletion_service import DeletionService

    doc = _make_doc()
    collection = _make_collection()
    model = _make_model()
    tenant = _make_tenant()

    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    call_count: list[int] = [0]

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        idx = call_count[0]
        call_count[0] += 1
        seq = [doc, collection, model]
        result.scalar_one_or_none = MagicMock(return_value=seq[idx] if idx < len(seq) else None)
        return result

    session.execute = _execute

    mock_retrieval = AsyncMock()
    mock_retrieval.delete_by_document = AsyncMock(return_value=5)

    mock_tenant_repo = AsyncMock()
    mock_tenant_repo.get_by_id = AsyncMock(return_value=tenant)

    mock_audit = AsyncMock()
    mock_audit.log = AsyncMock()

    with (
        patch("src.domain.deletion_service.TenantRepository", return_value=mock_tenant_repo),
        patch("src.domain.deletion_service.AuditService", return_value=mock_audit),
        patch("src.domain.deletion_service.get_minio_client") as mock_minio_factory,
        patch(
            "src.domain.deletion_service.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_thread,
    ):
        mock_minio = MagicMock()
        mock_minio_factory.return_value = mock_minio
        mock_thread.return_value = None

        svc = DeletionService(session)
        ctx = _make_ctx()

        await svc.delete_document(
            document_id=DOCUMENT_ID,
            ctx=ctx,
            retrieval_svc=mock_retrieval,
        )

    mock_retrieval.delete_by_document.assert_called_once()
    mock_thread.assert_called_once()
    session.delete.assert_called_once_with(doc)
    session.flush.assert_called()
    mock_audit.log.assert_called_once()
    audit_call = mock_audit.log.call_args
    assert audit_call.kwargs["action"] == "document.hard_deleted"
    assert audit_call.kwargs["resource_id"] == DOCUMENT_ID


@pytest.mark.asyncio
async def test_delete_document_not_found() -> None:
    """Raises NotFoundError when document does not exist."""
    from src.domain.deletion_service import DeletionService

    session = MagicMock()

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService"),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx()

        with pytest.raises(NotFoundError):
            await svc.delete_document(
                document_id=DOCUMENT_ID,
                ctx=ctx,
                retrieval_svc=AsyncMock(),
            )


@pytest.mark.asyncio
async def test_delete_document_wrong_tenant() -> None:
    """Raises NotFoundError when document belongs to a different tenant.

    The SQL query is scoped to ctx.tenant_id so a cross-tenant document is
    never returned — the mock simulates this by returning None, same as a
    real DB would for a correctly scoped query.
    """
    from src.domain.deletion_service import DeletionService

    session = MagicMock()

    async def _execute(q: object) -> MagicMock:
        # Simulates SQL query scoped to ctx.tenant_id — another tenant's doc returns None
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService"),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx(tenant_id=TENANT_ID)

        with pytest.raises(NotFoundError):
            await svc.delete_document(
                document_id=DOCUMENT_ID,
                ctx=ctx,
                retrieval_svc=AsyncMock(),
            )


@pytest.mark.asyncio
async def test_delete_document_qdrant_failure_swallowed() -> None:
    """Even if Qdrant raises QdrantUnavailableError, document is still deleted."""
    from src.domain.deletion_service import DeletionService
    from src.retrieval.exceptions import QdrantUnavailableError

    doc = _make_doc()
    collection = _make_collection()
    model = _make_model()
    tenant = _make_tenant()

    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    call_count: list[int] = [0]

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        idx = call_count[0]
        call_count[0] += 1
        seq = [doc, collection, model]
        result.scalar_one_or_none = MagicMock(return_value=seq[idx] if idx < len(seq) else None)
        return result

    session.execute = _execute

    mock_retrieval = AsyncMock()
    mock_retrieval.delete_by_document = AsyncMock(side_effect=QdrantUnavailableError("Qdrant down"))

    mock_tenant_repo = AsyncMock()
    mock_tenant_repo.get_by_id = AsyncMock(return_value=tenant)

    mock_audit = AsyncMock()
    mock_audit.log = AsyncMock()

    with (
        patch("src.domain.deletion_service.TenantRepository", return_value=mock_tenant_repo),
        patch("src.domain.deletion_service.AuditService", return_value=mock_audit),
        patch("src.domain.deletion_service.get_minio_client") as mock_minio_factory,
        patch(
            "src.domain.deletion_service.asyncio.to_thread", new_callable=AsyncMock
        ) as mock_thread,
    ):
        mock_minio_factory.return_value = MagicMock()
        mock_thread.return_value = None

        svc = DeletionService(session)
        ctx = _make_ctx()

        # Should NOT raise — Qdrant failure is swallowed
        await svc.delete_document(
            document_id=DOCUMENT_ID,
            ctx=ctx,
            retrieval_svc=mock_retrieval,
        )

    # Document still hard-deleted
    session.delete.assert_called_once_with(doc)
    mock_audit.log.assert_called_once()


# ── delete_conversation ──────────────────────────────────────────────────────


def _make_conversation(tenant_id: uuid.UUID = TENANT_ID, user_id: uuid.UUID = USER_ID) -> MagicMock:
    conv = MagicMock()
    conv.id = CONVERSATION_ID
    conv.tenant_id = tenant_id
    conv.user_id = user_id
    conv.is_deleted = False
    return conv


@pytest.mark.asyncio
async def test_delete_conversation_success() -> None:
    """Happy path: conversation is hard-deleted."""
    from src.domain.deletion_service import DeletionService

    conv = _make_conversation()
    session = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=conv)
        return result

    session.execute = _execute

    mock_audit = AsyncMock()
    mock_audit.log = AsyncMock()

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService", return_value=mock_audit),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx()

        await svc.delete_conversation(
            conversation_id=CONVERSATION_ID,
            user_id=USER_ID,
            ctx=ctx,
        )

    session.delete.assert_called_once_with(conv)
    session.flush.assert_called()
    mock_audit.log.assert_called_once()
    assert mock_audit.log.call_args.kwargs["action"] == "conversation.hard_deleted"


@pytest.mark.asyncio
async def test_delete_conversation_wrong_user() -> None:
    """NotFoundError raised when conversation belongs to a different user."""
    from src.domain.deletion_service import DeletionService

    other_user = uuid.uuid4()
    conv = _make_conversation(user_id=other_user)

    session = MagicMock()

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=conv)
        return result

    session.execute = _execute

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService"),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx()

        with pytest.raises(NotFoundError):
            await svc.delete_conversation(
                conversation_id=CONVERSATION_ID,
                user_id=USER_ID,  # ctx.user_id != conv.user_id
                ctx=ctx,
            )


@pytest.mark.asyncio
async def test_delete_conversation_not_found() -> None:
    """NotFoundError raised when conversation does not exist."""
    from src.domain.deletion_service import DeletionService

    session = MagicMock()

    async def _execute(q: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute

    with (
        patch("src.domain.deletion_service.TenantRepository"),
        patch("src.domain.deletion_service.AuditService"),
    ):
        svc = DeletionService(session)
        ctx = _make_ctx()

        with pytest.raises(NotFoundError):
            await svc.delete_conversation(
                conversation_id=CONVERSATION_ID,
                user_id=USER_ID,
                ctx=ctx,
            )
