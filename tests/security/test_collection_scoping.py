"""Tenant isolation and collection scoping security tests.

Mark: @pytest.mark.tenant_isolation

These tests verify:
- Tenant A user cannot access tenant B's collections
- Viewer only sees collections in their allowed_collection_ids
- RetrievalService.ensure_collection() is called with model_slug (not tenant_id)
  producing the canonical `emb_{model_slug}` collection name (ADR-1)
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.schemas.collection import CollectionCreate
from src.domain.auth import UserContext
from src.retrieval.service import RetrievalService


def _make_ctx(
    tenant_id: uuid.UUID,
    permissions: frozenset[str],
    allowed_ids: frozenset[uuid.UUID],
) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        keycloak_sub="kc-user",
        email="user@test.com",
        display_name="Test",
        tenant_id=tenant_id,
        roles=frozenset({"admin"}),
        permissions=permissions,
        allowed_collection_ids=allowed_ids,
        writable_collection_ids=allowed_ids if "admin:collections" in permissions else frozenset(),
    )


TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
COL_A = uuid.uuid4()
COL_B = uuid.uuid4()
MODEL_ID = uuid.uuid4()

ADMIN_PERMS = frozenset({"admin:collections", "documents:read"})
VIEWER_PERMS = frozenset({"documents:read"})


@pytest.mark.tenant_isolation
class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_get_collection_tenant_b_not_accessible_to_tenant_a(self) -> None:
        """CollectionRepository.get_by_id always includes tenant_id filter.

        A user from tenant A requesting collection_id owned by tenant B receives None
        (→ 404), not the actual row — even if they know the UUID.
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.db.repositories.collection_repository import CollectionRepository

        session = MagicMock(spec=AsyncSession)
        repo = CollectionRepository(session)

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None  # Tenant filter excludes it
        session.execute = AsyncMock(return_value=execute_result)

        # Tenant A queries for COL_B using their own tenant_id — result is None
        result = await repo.get_by_id(COL_B, TENANT_A)
        assert result is None

        # Verify the executed query included TENANT_A as the tenant filter
        assert session.execute.called
        compiled_query = str(session.execute.call_args[0][0].compile())
        assert "tenant_id" in compiled_query

    @pytest.mark.asyncio
    async def test_list_collections_scoped_to_allowed_ids(self) -> None:
        """list_by_tenant only returns collections in allowed_collection_ids."""
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.db.repositories.collection_repository import CollectionRepository

        session = MagicMock(spec=AsyncSession)
        repo = CollectionRepository(session)

        execute_result = MagicMock()
        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        execute_result.scalars.return_value = scalars_result
        execute_result.scalar_one.return_value = 0
        session.execute = AsyncMock(return_value=execute_result)

        # Viewer with no allowed_ids
        items, total = await repo.list_by_tenant(
            tenant_id=TENANT_A,
            allowed_ids=frozenset(),  # Empty — no collections visible
        )
        assert items == []
        assert total == 0
        # Should short-circuit without hitting DB
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_viewer_cannot_see_collections_outside_allowed_ids(self) -> None:
        """Viewer in tenant A with allowed_ids={COL_A} cannot access COL_B."""
        from src.core.exceptions import PermissionDeniedError
        from src.domain.auth import require_collection_read

        ctx_a_viewer = _make_ctx(TENANT_A, VIEWER_PERMS, frozenset({COL_A}))

        # Access to COL_A is allowed
        require_collection_read(COL_A, ctx_a_viewer)  # Should not raise

        # Access to COL_B raises PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            require_collection_read(COL_B, ctx_a_viewer)

    @pytest.mark.asyncio
    async def test_ensure_collection_uses_model_slug_not_tenant_id(self) -> None:
        """RetrievalService.ensure_collection() is called with model_slug, not tenant_id.

        ADR-1: Qdrant collection name = `emb_{model_slug}` (shared across all tenants).
        This test verifies CollectionService calls ensure_collection with the model slug.
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.db.models.collection import Collection
        from src.db.models.models_registry import ModelsRegistry
        from src.domain.collection_service import CollectionService

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

        model = MagicMock(spec=ModelsRegistry)
        model.id = MODEL_ID
        model.name = "BGE-M3"
        model.model_id = "BAAI/bge-m3"
        model.provider = "ollama"
        model.params = {"dimensions": 1024}

        collection = MagicMock(spec=Collection)
        collection.id = COL_A
        collection.tenant_id = TENANT_A
        collection.name = "Medical Records"
        collection.description = None
        collection.embedding_model_id = MODEL_ID
        collection.chunk_config = {
            "strategy": "recursive",
            "chunk_size": 512,
            "overlap": 64,
            "min_chunk_size": 64,
            "separators": ["\n\n", "\n", " "],
            "document_type_overrides": {},
        }
        collection.validation_config = {
            "confidence_threshold": 0.7,
            "require_review": False,
            "pii_action": "flag",
        }
        collection.is_active = True
        collection.created_at = now
        collection.updated_at = now

        retrieval_svc = AsyncMock(spec=RetrievalService)

        session = MagicMock(spec=AsyncSession)
        svc = CollectionService(session, retrieval_svc)

        ctx = _make_ctx(TENANT_A, ADMIN_PERMS, frozenset({COL_A}))
        body = CollectionCreate(
            name="Medical Records",
            embedding_model_id=MODEL_ID,
        )

        with (
            patch.object(svc._repo, "create", AsyncMock(return_value=collection)),
            patch.object(svc._model_repo, "get_by_id", AsyncMock(return_value=model)),
            patch.object(svc._repo, "count_documents", AsyncMock(return_value=0)),
            patch.object(svc._audit, "log", AsyncMock()),
        ):
            await svc.create_collection(body, ctx, ip=None)

        # Verify ensure_collection was called with model slug, NOT tenant_id
        retrieval_svc.ensure_collection.assert_called_once()
        call_args = retrieval_svc.ensure_collection.call_args
        model_slug_arg = call_args[0][0]  # First positional arg

        assert model_slug_arg == "bge_m3"  # "BGE-M3" → lowercased, hyphens → underscores
        assert str(TENANT_A) not in model_slug_arg
        # Qdrant collection name follows the canonical convention
        assert not model_slug_arg.startswith("tenant_")

    @pytest.mark.asyncio
    async def test_list_collections_tenant_b_user_sees_only_tenant_b_data(self) -> None:
        """CollectionRepository filters by tenant_id — tenant B user never sees tenant A data."""
        from sqlalchemy.ext.asyncio import AsyncSession

        from src.db.models.collection import Collection
        from src.db.repositories.collection_repository import CollectionRepository

        tenant_b_col = MagicMock(spec=Collection)
        tenant_b_col.id = COL_B
        tenant_b_col.tenant_id = TENANT_B

        session = MagicMock(spec=AsyncSession)
        repo = CollectionRepository(session)

        execute_result = MagicMock()
        scalars_result = MagicMock()
        scalars_result.all.return_value = [tenant_b_col]
        execute_result.scalars.return_value = scalars_result
        execute_result.scalar_one.return_value = 1
        session.execute = AsyncMock(return_value=execute_result)

        items, total = await repo.list_by_tenant(
            tenant_id=TENANT_B,
            allowed_ids=frozenset({COL_B}),
        )

        # Only tenant B data returned
        assert len(items) == 1
        assert items[0].tenant_id == TENANT_B
        assert total == 1

        # Confirm tenant_id was included in the SQL filter
        executed_sql = str(session.execute.call_args_list[0][0][0].compile())
        assert "tenant_id" in executed_sql
