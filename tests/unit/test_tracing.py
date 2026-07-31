"""Unit tests for Langfuse tracing integration.

GDPR reminder: tests must not assert on question/answer/chunk content.
All assertions are limited to decorator presence and safe configuration behaviour.

Coverage:
- langfuse_client module behaviour (disabled path, noop operations)
- @observe decorator presence on every graph node (query + ingest)
- update_span_metadata only receives GDPR-safe keys (no content, no PII)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

# PII / content keys that must NEVER appear in span metadata.
# Extend this set when new state fields are added.
_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "question",
        "answer",
        "rewritten_query",
        "chunk_text",
        "highlight_text",
        "text",
        "content",
        "extracted_text",
        "raw_bytes",
        "prompt",
        "history",
        "conversation_history",
        "response",
        "pii_values",
        "pii_matched",
    }
)


def _has_observe_decorator(func: object) -> bool:
    """Return True when *func* has been wrapped by langfuse.observe.

    The observe decorator sets __wrapped__ on the decorated function
    (standard Python decorator protocol via functools.wraps).
    """
    return hasattr(func, "__wrapped__")


def _assert_no_pii_in_metadata(metadata: dict) -> None:
    """Raise AssertionError if any forbidden key appears in *metadata*."""
    bad_keys = set(metadata.keys()) & _FORBIDDEN_METADATA_KEYS
    assert not bad_keys, f"Forbidden PII/content keys in span metadata: {bad_keys}"


# ---------------------------------------------------------------------------
# langfuse_client module tests
# ---------------------------------------------------------------------------


class TestLangfuseClientDisabled:
    """Tests for the disabled (no-keys) path."""

    def test_langfuse_disabled_when_no_keys(self) -> None:
        """get_langfuse() must return None when LANGFUSE_PUBLIC_KEY is absent."""
        # Force a fresh module state — patch env so no key is present.
        with patch.dict(os.environ, {}, clear=False):
            # Remove key if present in env
            env_backup = {}
            for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
                if key in os.environ:
                    env_backup[key] = os.environ.pop(key)

            # Re-import the module to pick up patched env (reset singleton).
            import src.core.langfuse_client as lf_mod

            # Manually set singleton to None to simulate cold start.
            lf_mod._langfuse_instance = None  # type: ignore[attr-defined]

            result = lf_mod.get_langfuse()

            # Restore env
            os.environ.update(env_backup)

            assert result is None

    def test_initialize_langfuse_noop_without_keys(self) -> None:
        """initialize_langfuse() must not raise when keys are absent."""
        import src.core.langfuse_client as lf_mod

        # Ensure singleton is None before the call.
        original = lf_mod._langfuse_instance  # type: ignore[attr-defined]
        lf_mod._langfuse_instance = None  # type: ignore[attr-defined]

        with patch("src.core.config.settings") as mock_settings:
            mock_settings.LANGFUSE_PUBLIC_KEY = None
            mock_settings.LANGFUSE_SECRET_KEY = None
            mock_settings.LANGFUSE_HOST = "http://localhost:3030"

            # Must not raise
            lf_mod.initialize_langfuse()

        assert lf_mod.get_langfuse() is None  # type: ignore[attr-defined]

        # Restore original singleton state
        lf_mod._langfuse_instance = original  # type: ignore[attr-defined]

    def test_shutdown_langfuse_noop_when_none(self) -> None:
        """shutdown_langfuse() must not raise when no client is initialized."""
        import src.core.langfuse_client as lf_mod

        original = lf_mod._langfuse_instance  # type: ignore[attr-defined]
        lf_mod._langfuse_instance = None  # type: ignore[attr-defined]

        # Must not raise
        lf_mod.shutdown_langfuse()

        lf_mod._langfuse_instance = original  # type: ignore[attr-defined]

    def test_update_span_metadata_noop_when_disabled(self) -> None:
        """update_span_metadata() must be a no-op when langfuse is disabled."""
        import src.core.langfuse_client as lf_mod

        original = lf_mod._langfuse_instance  # type: ignore[attr-defined]
        lf_mod._langfuse_instance = None  # type: ignore[attr-defined]

        # Must not raise even with arbitrary metadata
        lf_mod.update_span_metadata({"tenant_id": "abc", "chunk_count": 5})

        lf_mod._langfuse_instance = original  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Decorator presence tests — query graph
# ---------------------------------------------------------------------------


class TestObserveDecoratorsPresent:
    """Verify that every query-graph node function is wrapped with @observe."""

    def test_node_classify_intent_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_classify_intent import node_classify_intent

        assert _has_observe_decorator(node_classify_intent), (
            "node_classify_intent must be decorated with @observe"
        )

    def test_node_rewrite_query_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_rewrite_query import node_rewrite_query

        assert _has_observe_decorator(node_rewrite_query), (
            "node_rewrite_query must be decorated with @observe"
        )

    def test_node_retrieve_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_retrieve import node_retrieve

        assert _has_observe_decorator(node_retrieve), (
            "node_retrieve must be decorated with @observe"
        )

    def test_node_grade_documents_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_grade_documents import node_grade_documents

        assert _has_observe_decorator(node_grade_documents), (
            "node_grade_documents must be decorated with @observe"
        )

    def test_node_rerank_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_rerank import node_rerank

        assert _has_observe_decorator(node_rerank), "node_rerank must be decorated with @observe"

    def test_node_generate_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_generate import node_generate

        assert _has_observe_decorator(node_generate), (
            "node_generate must be decorated with @observe"
        )

    def test_node_guardrails_output_has_observe(self) -> None:
        from src.graphs.query_graph.nodes.node_guardrails_output import node_guardrails_output

        assert _has_observe_decorator(node_guardrails_output), (
            "node_guardrails_output must be decorated with @observe"
        )

    def test_observe_decorators_present_on_all_query_nodes(self) -> None:
        """Aggregate test: all query graph nodes must carry the @observe decorator."""
        from src.graphs.query_graph.nodes.node_classify_intent import node_classify_intent
        from src.graphs.query_graph.nodes.node_generate import node_generate
        from src.graphs.query_graph.nodes.node_grade_documents import node_grade_documents
        from src.graphs.query_graph.nodes.node_guardrails_output import node_guardrails_output
        from src.graphs.query_graph.nodes.node_rerank import node_rerank
        from src.graphs.query_graph.nodes.node_retrieve import node_retrieve
        from src.graphs.query_graph.nodes.node_rewrite_query import node_rewrite_query

        nodes = {
            "node_classify_intent": node_classify_intent,
            "node_rewrite_query": node_rewrite_query,
            "node_retrieve": node_retrieve,
            "node_grade_documents": node_grade_documents,
            "node_rerank": node_rerank,
            "node_generate": node_generate,
            "node_guardrails_output": node_guardrails_output,
        }
        missing = [name for name, fn in nodes.items() if not _has_observe_decorator(fn)]
        assert not missing, f"Missing @observe decorator on: {missing}"


# ---------------------------------------------------------------------------
# Decorator presence tests — ingest graph
# ---------------------------------------------------------------------------


class TestIngestObserveDecoratorsPresent:
    """Verify that every ingest-graph node function is wrapped with @observe."""

    def test_node_fetch_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_fetch import node_fetch

        assert _has_observe_decorator(node_fetch), "node_fetch must be decorated with @observe"

    def test_node_extract_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_extract import node_extract

        assert _has_observe_decorator(node_extract), "node_extract must be decorated with @observe"

    def test_node_dedupe_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_dedupe import node_dedupe

        assert _has_observe_decorator(node_dedupe), "node_dedupe must be decorated with @observe"

    def test_node_validate_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_validate import node_validate

        assert _has_observe_decorator(node_validate), (
            "node_validate must be decorated with @observe"
        )

    def test_node_pii_scan_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_pii_scan import node_pii_scan

        assert _has_observe_decorator(node_pii_scan), (
            "node_pii_scan must be decorated with @observe"
        )

    def test_node_chunk_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_chunk import node_chunk

        assert _has_observe_decorator(node_chunk), "node_chunk must be decorated with @observe"

    def test_node_embed_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_embed import node_embed

        assert _has_observe_decorator(node_embed), "node_embed must be decorated with @observe"

    def test_node_upsert_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_upsert import node_upsert

        assert _has_observe_decorator(node_upsert), "node_upsert must be decorated with @observe"

    def test_node_persist_has_observe(self) -> None:
        from src.graphs.ingest_graph.nodes.node_persist import node_persist

        assert _has_observe_decorator(node_persist), "node_persist must be decorated with @observe"

    def test_observe_decorators_present_on_all_ingest_nodes(self) -> None:
        """Aggregate test: all ingest graph nodes must carry the @observe decorator."""
        from src.graphs.ingest_graph.nodes.node_chunk import node_chunk
        from src.graphs.ingest_graph.nodes.node_dedupe import node_dedupe
        from src.graphs.ingest_graph.nodes.node_embed import node_embed
        from src.graphs.ingest_graph.nodes.node_extract import node_extract
        from src.graphs.ingest_graph.nodes.node_fetch import node_fetch
        from src.graphs.ingest_graph.nodes.node_persist import node_persist
        from src.graphs.ingest_graph.nodes.node_pii_scan import node_pii_scan
        from src.graphs.ingest_graph.nodes.node_upsert import node_upsert
        from src.graphs.ingest_graph.nodes.node_validate import node_validate

        nodes = {
            "node_fetch": node_fetch,
            "node_extract": node_extract,
            "node_dedupe": node_dedupe,
            "node_validate": node_validate,
            "node_pii_scan": node_pii_scan,
            "node_chunk": node_chunk,
            "node_embed": node_embed,
            "node_upsert": node_upsert,
            "node_persist": node_persist,
        }
        missing = [name for name, fn in nodes.items() if not _has_observe_decorator(fn)]
        assert not missing, f"Missing @observe decorator on: {missing}"


# ---------------------------------------------------------------------------
# invoke_query_graph
# ---------------------------------------------------------------------------


class TestInvokeQueryGraphHasObserve:
    """Verify that invoke_query_graph is wrapped with @observe."""

    def test_invoke_query_graph_has_observe(self) -> None:
        from src.graphs.query_graph.graph import invoke_query_graph

        assert _has_observe_decorator(invoke_query_graph), (
            "invoke_query_graph must be decorated with @observe"
        )


# ---------------------------------------------------------------------------
# GDPR: no PII in span metadata — update_span_metadata call inspection
# ---------------------------------------------------------------------------


class TestSpanMetadataGdprCompliance:
    """Verify that update_span_metadata never receives content or PII keys.

    We patch `src.core.langfuse_client.update_span_metadata` in each node's
    module namespace and run the node with mocked dependencies.  Then we
    inspect every call's `metadata` kwarg for forbidden keys.

    Only the "fast paths" that don't require real DB/LLM are covered here
    (e.g. the no_chunks branch in node_grade_documents, the skip branch in
    node_rerank, the clean-pass branch in node_guardrails_output).  Full-path
    integration coverage is handled in tests/integration/.
    """

    def test_update_span_metadata_noop_does_not_receive_content(self) -> None:
        """Sanity: calling update_span_metadata with safe keys passes the GDPR check."""
        safe_metadata = {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "chunk_count": 5,
            "latency_ms": 42,
            "model_id": "abc",
            "no_results": False,
        }
        _assert_no_pii_in_metadata(safe_metadata)

    def test_forbidden_content_key_is_detected(self) -> None:
        """Sanity: the helper correctly flags content keys."""
        bad_metadata = {
            "tenant_id": "abc",
            "question": "What is the meaning of life?",  # forbidden
        }
        with pytest.raises(AssertionError, match="question"):
            _assert_no_pii_in_metadata(bad_metadata)

    @pytest.mark.asyncio
    async def test_node_guardrails_output_span_no_content(self) -> None:
        """node_guardrails_output span must not include answer content."""
        import uuid

        from src.graphs.query_graph.state import QueryState

        state = QueryState(
            question="test",
            conversation_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            llm_model_id=uuid.uuid4(),
            halt=False,
            no_results=True,
            answer="",
            intent="topical",
            guardrails_config={},
            collection_ids=[],
            allowed_collection_ids=[],
            conversation_history=[],
        )

        captured_calls: list[dict] = []

        def _fake_update(metadata: dict) -> None:
            captured_calls.append(metadata)

        with patch(
            "src.graphs.query_graph.nodes.node_guardrails_output._lf_update_span",
            side_effect=_fake_update,
        ):
            from src.graphs.query_graph.nodes.node_guardrails_output import (
                node_guardrails_output,
            )

            # Call the underlying function (bypass @observe wrapper)
            wrapped = node_guardrails_output.__wrapped__  # type: ignore[attr-defined]
            await wrapped(state, {"configurable": {}})

        assert captured_calls, "update_span_metadata was not called"
        for metadata in captured_calls:
            _assert_no_pii_in_metadata(metadata)
            # Required safe keys must be present
            assert "tenant_id" in metadata
            assert "case_applied" in metadata

    @pytest.mark.asyncio
    async def test_node_rerank_skip_span_no_content(self) -> None:
        """node_rerank span on the skip path must not include chunk content."""
        import uuid

        from src.graphs.query_graph.state import QueryState

        state = QueryState(
            question="test",
            conversation_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            llm_model_id=uuid.uuid4(),
            rerank_enabled=False,
            retrieved_chunks=[{"point_id": "abc", "highlight_text": "some text", "score": 0.9}],
            collection_ids=[],
            allowed_collection_ids=[],
            conversation_history=[],
            guardrails_config={},
        )

        captured_calls: list[dict] = []

        def _fake_update(metadata: dict) -> None:
            captured_calls.append(metadata)

        with patch(
            "src.graphs.query_graph.nodes.node_rerank._lf_update_span",
            side_effect=_fake_update,
        ):
            from src.graphs.query_graph.nodes.node_rerank import node_rerank

            wrapped = node_rerank.__wrapped__  # type: ignore[attr-defined]
            await wrapped(state, {"configurable": {}})

        assert captured_calls, "update_span_metadata was not called on skip path"
        for metadata in captured_calls:
            _assert_no_pii_in_metadata(metadata)
            assert "tenant_id" in metadata
            assert "chunk_count_in" in metadata
            assert "rerank_enabled" in metadata
            assert metadata["rerank_enabled"] is False

    @pytest.mark.asyncio
    async def test_node_grade_documents_no_chunks_span(self) -> None:
        """node_grade_documents span on the empty-chunks path must be GDPR-safe."""
        import uuid

        from src.graphs.query_graph.state import QueryState

        state = QueryState(
            question="test",
            conversation_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            llm_model_id=uuid.uuid4(),
            retrieved_chunks=[],
            collection_ids=[],
            allowed_collection_ids=[],
            conversation_history=[],
            guardrails_config={},
        )

        captured_calls: list[dict] = []

        def _fake_update(metadata: dict) -> None:
            captured_calls.append(metadata)

        with patch(
            "src.graphs.query_graph.nodes.node_grade_documents._lf_update_span",
            side_effect=_fake_update,
        ):
            from src.graphs.query_graph.nodes.node_grade_documents import node_grade_documents

            wrapped = node_grade_documents.__wrapped__  # type: ignore[attr-defined]
            await wrapped(state, {"configurable": {"llm": MagicMock(), "db": MagicMock()}})

        assert captured_calls, "update_span_metadata was not called"
        for metadata in captured_calls:
            _assert_no_pii_in_metadata(metadata)
            assert "tenant_id" in metadata
            assert metadata["total_count"] == 0
            assert metadata["relevant_count"] == 0

    @pytest.mark.asyncio
    async def test_node_dedupe_unique_span_no_content(self) -> None:
        """node_dedupe span on the unique path must only contain GDPR-safe keys."""
        import uuid
        from unittest.mock import AsyncMock

        from src.graphs.ingest_graph.state import IngestState

        doc_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        state = IngestState(
            document_id=doc_id,
            tenant_id=tenant_id,
            collection_id=uuid.uuid4(),
            minio_key="test/key",
            job_id=uuid.uuid4(),
            sha256="abc123",
        )

        captured_calls: list[dict] = []

        def _fake_update(metadata: dict) -> None:
            captured_calls.append(metadata)

        # Async-compatible mock for session.execute
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # no duplicate

        mock_session = MagicMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.commit = AsyncMock()

        async def _fake_update_step(*args: object, **kwargs: object) -> None:
            pass

        with (
            patch(
                "src.graphs.ingest_graph.nodes.node_dedupe._lf_update_span",
                side_effect=_fake_update,
            ),
            patch(
                "src.graphs.ingest_graph.nodes.node_dedupe.update_step",
                new=_fake_update_step,
            ),
        ):
            from src.graphs.ingest_graph.nodes.node_dedupe import node_dedupe

            wrapped = node_dedupe.__wrapped__  # type: ignore[attr-defined]
            await wrapped(state, {"configurable": {"db": mock_session}})

        assert captured_calls, "update_span_metadata was not called"
        for metadata in captured_calls:
            _assert_no_pii_in_metadata(metadata)
            assert "document_id" in metadata
            assert "tenant_id" in metadata
            assert metadata["result"] == "unique"

    def test_pii_scan_span_keys_are_safe(self) -> None:
        """Verify statically that node_pii_scan's metadata dicts use only safe keys."""
        # Read the source to confirm _lf_update_span calls don't include raw text.
        import inspect

        from src.graphs.ingest_graph.nodes import node_pii_scan as mod

        source = inspect.getsource(mod)
        # The pii_type_labels list (which could expose type+location info as a set)
        # must not be passed as a list value — only the count.
        # We allow pii_type_labels being used in pii_flags (the ValidationResult field),
        # but it must not appear as a direct span metadata value.
        assert '"pii_type_labels"' not in source, (
            "pii_type_labels (the raw list) must not appear as a span metadata key"
        )
        # The actual text must never be in the span
        assert '"text"' not in source.split("_lf_update_span")[1].split(")")[0], (
            "raw text content must not appear in the pii_scan span"
        )

    def test_node_generate_source_has_no_answer_in_span(self) -> None:
        """Verify statically that node_generate never sends answer content to Langfuse."""
        import inspect

        from src.graphs.query_graph.nodes import node_generate as mod

        source = inspect.getsource(mod)
        # Find the _lf_update_span call and check it doesn't contain 'answer'
        span_call_sections = source.split("_lf_update_span(")
        for section in span_call_sections[1:]:  # skip content before first call
            # Extract content up to the closing paren of the metadata dict
            call_body = section.split(")\n")[0]
            assert '"answer"' not in call_body, (
                "answer content must not appear in node_generate span metadata"
            )

    def test_node_retrieve_span_has_no_query_text(self) -> None:
        """Verify statically that node_retrieve never sends query text to Langfuse."""
        import inspect

        from src.graphs.query_graph.nodes import node_retrieve as mod

        source = inspect.getsource(mod)
        span_call_sections = source.split("_lf_update_span(")
        for section in span_call_sections[1:]:
            call_body = section.split(")\n")[0]
            assert '"rewritten_query"' not in call_body, (
                "rewritten_query text must not appear in node_retrieve span metadata"
            )
            assert '"question"' not in call_body, (
                "question text must not appear in node_retrieve span metadata"
            )
