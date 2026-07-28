"""Security tests for invoke_query_graph pipeline-collection authorization check.

Verifies that BLOCKER-2 is closed: pipeline.collection_ids that fall outside
ctx.allowed_collection_ids are rejected with TenantIsolationError before the
graph is executed.

Mark: @pytest.mark.tenant_isolation
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from src.core.exceptions import TenantIsolationError
from src.db.models.rag_pipeline import RagPipeline
from src.domain.auth import UserContext
from src.graphs.query_graph.graph import _assert_pipeline_collections_authorized

TENANT_ID = uuid.uuid4()
COL_ALLOWED = uuid.uuid4()
COL_FORBIDDEN = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()


def _make_ctx(allowed: frozenset[uuid.UUID]) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(),
        keycloak_sub="kc-user",
        email="user@clinic.test",
        display_name="User",
        tenant_id=TENANT_ID,
        roles=frozenset({"viewer"}),
        permissions=frozenset({"chat:query"}),
        allowed_collection_ids=allowed,
        writable_collection_ids=frozenset(),
    )


def _make_pipeline(collection_ids: list[uuid.UUID]) -> MagicMock:
    pipeline = MagicMock(spec=RagPipeline)
    pipeline.id = PIPELINE_ID
    pipeline.tenant_id = TENANT_ID
    pipeline.collection_ids = collection_ids
    pipeline.llm_model_id = LLM_MODEL_ID
    pipeline.prompt_config = {}
    pipeline.guardrails = {}
    pipeline.is_active = True
    return pipeline


@pytest.mark.tenant_isolation
class TestAssertPipelineCollectionsAuthorized:
    def test_subset_passes(self) -> None:
        """Pipeline referencing only allowed collections must not raise."""
        ctx = _make_ctx(frozenset({COL_ALLOWED}))
        pipeline = _make_pipeline([COL_ALLOWED])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_exact_match_passes(self) -> None:
        """Pipeline with exactly ctx.allowed_collection_ids must not raise."""
        col_a = uuid.uuid4()
        col_b = uuid.uuid4()
        ctx = _make_ctx(frozenset({col_a, col_b}))
        pipeline = _make_pipeline([col_a, col_b])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_forbidden_collection_raises_tenant_isolation_error(self) -> None:
        """Pipeline referencing a collection outside allowed_collection_ids raises TenantIsolationError."""  # noqa: E501
        ctx = _make_ctx(frozenset({COL_ALLOWED}))
        pipeline = _make_pipeline([COL_ALLOWED, COL_FORBIDDEN])
        with pytest.raises(TenantIsolationError):
            _assert_pipeline_collections_authorized(pipeline, ctx)

    def test_entirely_foreign_collection_raises(self) -> None:
        """Pipeline with zero overlap to allowed_collection_ids raises TenantIsolationError."""
        ctx = _make_ctx(frozenset({COL_ALLOWED}))
        pipeline = _make_pipeline([COL_FORBIDDEN])
        with pytest.raises(TenantIsolationError):
            _assert_pipeline_collections_authorized(pipeline, ctx)

    def test_empty_allowed_collection_ids_skips_check(self) -> None:
        """Empty allowed_collection_ids means 'no restriction' (admin). Check is skipped."""
        ctx = _make_ctx(frozenset())  # admin — no restrictions
        pipeline = _make_pipeline([COL_ALLOWED, COL_FORBIDDEN, uuid.uuid4()])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_empty_pipeline_collection_ids_always_passes(self) -> None:
        """Pipeline with no collections is a subset of any set; must not raise."""
        ctx = _make_ctx(frozenset({COL_ALLOWED}))
        pipeline = _make_pipeline([])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_raises_tenant_isolation_not_permission_denied(self) -> None:
        """The raised exception must be TenantIsolationError, never PermissionDeniedError.

        TenantIsolationError always maps to HTTP 403 without leaking resource existence.
        """
        ctx = _make_ctx(frozenset({COL_ALLOWED}))
        pipeline = _make_pipeline([COL_FORBIDDEN])
        from src.core.exceptions import PermissionDeniedError

        with pytest.raises(TenantIsolationError):
            _assert_pipeline_collections_authorized(pipeline, ctx)

        # Ensure it is NOT the weaker PermissionDeniedError
        try:
            _assert_pipeline_collections_authorized(pipeline, ctx)
        except TenantIsolationError:
            pass
        except PermissionDeniedError:
            pytest.fail("Should raise TenantIsolationError, not PermissionDeniedError")
