"""node_guardrails_output — rule-based output filter with optional LLM safety pass.

Handles four cases (evaluated in order):
1. halt=True (chitchat / out_of_scope) → return Polish out-of-scope message.
2. no_results=True → return Polish "not found" message.
3. add_disclaimer=True in guardrails_config → append medical disclaimer to answer.
4. Otherwise → pass state.answer through unchanged.

After the deterministic pass, an optional LLM-based guardrail check runs when
``guardrails_config["llm_guardrails_enabled"] == True``.  The LLM evaluates the
answer against three safety criteria (unsafe medical advice, PII, prompt injection
artefacts) and returns a ``GuardrailsDecision``.  If ``safe=False``, the answer is
replaced with a safe refusal.  The LLM pass is fail-open: any error is logged as a
warning and the pipeline continues with the current answer.

GDPR: Never log answer content. Log only tenant_id and the case applied.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe
from pydantic import BaseModel

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_DISCLAIMER_PATH = Path(__file__).parent.parent.parent / "prompts" / "disclaimer_medical_pl_v1.md"
_GUARDRAILS_PROMPT_PATH = (
    Path(__file__).parent.parent.parent / "prompts" / "guardrails_output_v1.md"
)

_OUT_OF_SCOPE_ANSWER = "To pytanie wykracza poza zakres dokumentów dostępnych w systemie."

_NO_RESULTS_ANSWER = (
    "Nie znalazłem odpowiedzi w dostępnych dokumentach. "
    "Jeśli szukasz informacji o konkretnej procedurze lub dokumencie, "
    "upewnij się, że plik został dodany do systemu i zatwierdzony przez administratora."
)

_LLM_REFUSAL_ANSWER = (
    "Odpowiedź została zablokowana przez system bezpieczeństwa. "
    "Skontaktuj się z administratorem systemu lub personelem medycznym."
)

_disclaimer_cache: str | None = None
_guardrails_prompt_cache: str | None = None


class GuardrailsDecision(BaseModel):
    """Structured output from the LLM safety evaluation pass."""

    safe: bool
    reason_code: str | None = None  # categorical, not free text


def _load_disclaimer() -> str:
    global _disclaimer_cache
    if _disclaimer_cache is None:
        _disclaimer_cache = _DISCLAIMER_PATH.read_text(encoding="utf-8").strip()
    return _disclaimer_cache


def _load_guardrails_prompt() -> str:
    global _guardrails_prompt_cache
    if _guardrails_prompt_cache is None:
        _guardrails_prompt_cache = _GUARDRAILS_PROMPT_PATH.read_text(encoding="utf-8")
    return _guardrails_prompt_cache


async def _run_llm_guardrails(
    answer: str,
    state: QueryState,
    config: dict[str, Any],
) -> GuardrailsDecision:
    """Call the LLM guardrails prompt and return a structured safety decision.

    Fail-open: any exception is re-raised so the caller can log and continue.

    Args:
        answer: The generated answer text to evaluate.
        state: Full pipeline state (used for model resolution).
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        GuardrailsDecision with safe=True/False and optional reason.

    Raises:
        Exception: Any LLM or parse failure — caller handles fail-open logic.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise ValueError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    prompt = _load_guardrails_prompt().replace("{{ANSWER}}", answer)

    response = await llm.chat_completion(
        model=model_record.model_id,
        messages=[
            {"role": "system", "content": prompt},
        ],
        base_url=model_record.endpoint_url,
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = response.choices[0].message.content or ""
    parsed = json.loads(raw)
    return GuardrailsDecision(
        safe=parsed.get("safe", True),
        reason_code=parsed.get("reason_code"),
    )


@observe(capture_input=False, capture_output=False)
async def node_guardrails_output(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Apply guardrails to the pipeline output.

    Args:
        state: Full pipeline state.
        config: RunnableConfig; must contain configurable["llm"] and configurable["db"]
            when ``state.guardrails_config["llm_guardrails_enabled"]`` is True.

    Returns:
        {"answer": str}
    """
    case_applied: str
    llm_guardrails_triggered = False

    # --- Deterministic pass -------------------------------------------------
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

    # --- Optional LLM safety pass -------------------------------------------
    # Only runs when explicitly enabled and there is a non-empty answer to check.
    # Short-circuits that already replaced the answer (out_of_scope, no_results)
    # are not checked — the replacement strings are known-safe.
    llm_enabled = state.guardrails_config.get("llm_guardrails_enabled", False)
    if llm_enabled and case_applied in ("disclaimer_appended", "passthrough") and answer:
        try:
            decision = await _run_llm_guardrails(answer, state, config)
            if not decision.safe:
                answer = _LLM_REFUSAL_ANSWER
                llm_guardrails_triggered = True
                logger.warning(
                    "node_guardrails_output.llm_guardrails_blocked",
                    tenant_id=str(state.tenant_id),
                    reason_code=decision.reason_code,
                )
        except Exception as exc:
            # Fail-open: log and continue with current answer.
            logger.warning(
                "node_guardrails_output.llm_guardrails_error",
                tenant_id=str(state.tenant_id),
                error=type(exc).__name__,
            )

    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "case_applied": case_applied,
            "llm_guardrails_enabled": llm_enabled,
            "llm_guardrails_triggered": llm_guardrails_triggered,
        }
    )
    logger.info(
        "node_guardrails_output.completed",
        tenant_id=str(state.tenant_id),
        case=case_applied,
        llm_guardrails_enabled=llm_enabled,
        llm_guardrails_triggered=llm_guardrails_triggered,
    )
    return {"answer": answer}
