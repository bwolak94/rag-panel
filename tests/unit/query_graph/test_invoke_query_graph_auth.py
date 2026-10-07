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


PUBLIC_COL = uuid.uuid4()


def _make_ctx(
    allowed: frozenset[uuid.UUID],
    public: frozenset[uuid.UUID] | None = None,
) -> UserContext:
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
        public_collection_ids=public or frozenset(),
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

    def test_pipeline_with_public_collection_passes_for_normal_user(self) -> None:
        """Pipeline referencing a public collection must not raise for a user without a
        private CollectionAccess grant to it — public collections are accessible to all
        tenants without explicit grants (ADR-020)."""
        public_col = uuid.uuid4()
        ctx = _make_ctx(
            allowed=frozenset({COL_ALLOWED}),
            public=frozenset({public_col}),
        )
        pipeline = _make_pipeline([public_col])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_pipeline_with_mixed_private_and_public_collections_passes(self) -> None:
        """Pipeline using both a private and a public collection passes when user
        has a grant to the private one and public_collection_ids covers the public one."""
        public_col = uuid.uuid4()
        ctx = _make_ctx(
            allowed=frozenset({COL_ALLOWED}),
            public=frozenset({public_col}),
        )
        pipeline = _make_pipeline([COL_ALLOWED, public_col])
        _assert_pipeline_collections_authorized(pipeline, ctx)  # must not raise

    def test_pipeline_with_public_collection_unknown_to_user_is_rejected(self) -> None:
        """A collection that is neither in allowed_collection_ids nor public_collection_ids
        is rejected even if the platform considers it public — the user's context must
        have it in public_collection_ids to gain access."""
        unknown_col = uuid.uuid4()
        ctx = _make_ctx(
            allowed=frozenset({COL_ALLOWED}),
            public=frozenset(),  # this user's context has no public collections
        )
        pipeline = _make_pipeline([unknown_col])
        with pytest.raises(TenantIsolationError):
            _assert_pipeline_collections_authorized(pipeline, ctx)

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


# ---------------------------------------------------------------------------
# ADR-020: public_collection_ids seeded into QueryState from UserContext
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestPublicCollectionIdsSeededIntoState:
    """invoke_query_graph seeds public_collection_ids from UserContext into QueryState."""

    @pytest.mark.asyncio
    async def test_public_collection_ids_included_in_initial_state(self) -> None:
        """QueryState.public_collection_ids receives values from UserContext (ADR-020)."""
        from unittest.mock import AsyncMock, patch

        from src.graphs.query_graph.graph import invoke_query_graph

        ctx = _make_ctx(frozenset({COL_ALLOWED}), public=frozenset({PUBLIC_COL}))
        pipeline = _make_pipeline([COL_ALLOWED])

        captured_state: dict = {}

        async def fake_ainvoke(state: dict, config: dict) -> dict:
            captured_state.update(state)
            return {
                "answer": "ok",
                "citations": [],
                "no_results": False,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "context_tokens_used": 0,
                "chunks_included": 0,
            }

        with (
            patch("src.graphs.query_graph.graph.build_query_graph") as mock_build,
            patch("src.graphs.query_graph.graph._lf_update_span"),
        ):
            mock_graph = AsyncMock()
            mock_graph.ainvoke = fake_ainvoke
            mock_build.return_value = mock_graph

            await invoke_query_graph(
                question="test",
                pipeline=pipeline,
                ctx=ctx,
                db=AsyncMock(),
                llm=AsyncMock(),
                retrieval=AsyncMock(),
                conversation_history=[],
            )

        assert PUBLIC_COL in captured_state.get("public_collection_ids", [])

    @pytest.mark.asyncio
    async def test_empty_public_collection_ids_when_user_has_none(self) -> None:
        """QueryState.public_collection_ids is empty when UserContext has none."""
        from unittest.mock import AsyncMock, patch

        from src.graphs.query_graph.graph import invoke_query_graph

        ctx = _make_ctx(frozenset({COL_ALLOWED}))  # no public collections
        pipeline = _make_pipeline([COL_ALLOWED])

        captured_state: dict = {}

        async def fake_ainvoke(state: dict, config: dict) -> dict:
            captured_state.update(state)
            return {
                "answer": "ok",
                "citations": [],
                "no_results": False,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "context_tokens_used": 0,
                "chunks_included": 0,
            }

        with (
            patch("src.graphs.query_graph.graph.build_query_graph") as mock_build,
            patch("src.graphs.query_graph.graph._lf_update_span"),
        ):
            mock_graph = AsyncMock()
            mock_graph.ainvoke = fake_ainvoke
            mock_build.return_value = mock_graph

            await invoke_query_graph(
                question="test",
                pipeline=pipeline,
                ctx=ctx,
                db=AsyncMock(),
                llm=AsyncMock(),
                retrieval=AsyncMock(),
                conversation_history=[],
            )

        assert captured_state.get("public_collection_ids") == []
