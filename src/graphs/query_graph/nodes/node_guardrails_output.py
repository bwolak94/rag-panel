"""node_guardrails_output — rule-based output filter. No LLM call.

Handles three cases:
1. halt=True (chitchat / out_of_scope) → return Polish out-of-scope message.
2. no_results=True → return Polish "not found" message.
3. add_disclaimer=True in guardrails_config → append medical disclaimer to answer.
4. Otherwise → pass state.answer through unchanged.

GDPR: Never log answer content. Log only tenant_id and the case applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_DISCLAIMER_PATH = Path(__file__).parent.parent.parent / "prompts" / "disclaimer_medical_pl_v1.md"

_OUT_OF_SCOPE_ANSWER = "To pytanie wykracza poza zakres dokumentów dostępnych w systemie."

_NO_RESULTS_ANSWER = (
    "Nie znalazłem odpowiedzi w dostępnych dokumentach. "
    "Jeśli szukasz informacji o konkretnej procedurze lub dokumencie, "
    "upewnij się, że plik został dodany do systemu i zatwierdzony przez administratora."
)

_disclaimer_cache: str | None = None


def _load_disclaimer() -> str:
    global _disclaimer_cache
    if _disclaimer_cache is None:
        _disclaimer_cache = _DISCLAIMER_PATH.read_text(encoding="utf-8").strip()
    return _disclaimer_cache


@observe(capture_input=False, capture_output=False)
async def node_guardrails_output(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG001
    """Apply guardrails to the pipeline output.

    Args:
        state: Full pipeline state.
        config: RunnableConfig (not used — rule-based, no LLM call).

    Returns:
        {"answer": str}
    """
    case_applied: str

    if state.halt and state.intent in ("chitchat", "out_of_scope"):
        answer = _OUT_OF_SCOPE_ANSWER
        case_applied = "out_of_scope"

    elif state.no_results:
        answer = _NO_RESULTS_ANSWER
        case_applied = "no_results"

    elif state.guardrails_config.get("add_disclaimer") and state.answer:
        disclaimer = _load_disclaimer()
        answer = f"{state.answer}\n\n{disclaimer}"
        case_applied = "disclaimer_appended"

    else:
        answer = state.answer or ""
        case_applied = "passthrough"

    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
        }
    )
    logger.info(
        "node_guardrails_output.completed",
        tenant_id=str(state.tenant_id),
        case=case_applied,
    )
    return {"answer": answer}
