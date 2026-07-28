"""Unit tests for Langfuse tracing integration.

GDPR reminder: tests must not assert on question/answer/chunk content.
All assertions are limited to decorator presence and safe configuration behaviour.
"""

from __future__ import annotations

import os
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _has_observe_decorator(func: object) -> bool:
    """Return True when *func* has been wrapped by langfuse.observe.

    The observe decorator sets __wrapped__ on the decorated function
    (standard Python decorator protocol via functools.wraps).
    """
    return hasattr(func, "__wrapped__")


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


# ---------------------------------------------------------------------------
# Decorator presence tests
# ---------------------------------------------------------------------------


class TestObserveDecoratorsPresent:
    """Verify that every node function is wrapped with @observe."""

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

    def test_observe_decorators_present_on_all_nodes(self) -> None:
        """Aggregate test: all 6 nodes must carry the @observe decorator."""
        from src.graphs.query_graph.nodes.node_classify_intent import node_classify_intent
        from src.graphs.query_graph.nodes.node_generate import node_generate
        from src.graphs.query_graph.nodes.node_grade_documents import node_grade_documents
        from src.graphs.query_graph.nodes.node_guardrails_output import node_guardrails_output
        from src.graphs.query_graph.nodes.node_retrieve import node_retrieve
        from src.graphs.query_graph.nodes.node_rewrite_query import node_rewrite_query

        nodes = {
            "node_classify_intent": node_classify_intent,
            "node_rewrite_query": node_rewrite_query,
            "node_retrieve": node_retrieve,
            "node_grade_documents": node_grade_documents,
            "node_generate": node_generate,
            "node_guardrails_output": node_guardrails_output,
        }
        missing = [name for name, fn in nodes.items() if not _has_observe_decorator(fn)]
        assert not missing, f"Missing @observe decorator on: {missing}"


class TestInvokeQueryGraphHasObserve:
    """Verify that invoke_query_graph is wrapped with @observe."""

    def test_invoke_query_graph_has_observe(self) -> None:
        from src.graphs.query_graph.graph import invoke_query_graph

        assert _has_observe_decorator(invoke_query_graph), (
            "invoke_query_graph must be decorated with @observe"
        )
