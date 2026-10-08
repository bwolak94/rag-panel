"""node_guardrails_output — rule-based output filter with optional LLM safety pass.

Handles four cases (evaluated in order):
1. halt=True (chitchat / out_of_scope) → return Polish out-of-scope message.
2. no_results=True → return Polish "not found" message.
3. add_disclaimer=True in guardrails_config → append medical disclaimer to answer.
4. Otherwise → pass state.answer through unchanged.

After the deterministic pass, an optional LLM-based guardrail check runs when
``guardrails_config["llm_guardrails_enabled"] == True`` (v1, legacy) or
``guardrails_config["llm_check"] == True`` (ADR-017 v2).

v1 path: evaluates safety only (safe/reason_code), uses guardrails_output_v1.md.
v2 path: evaluates safety + factual grounding + PII, returns full GuardrailsDecision
         with modifications, disclaimer_added, pii_detected, and reasoning fields.
         reasoning is NEVER logged (GDPR). 30 s timeout; fail-open on error/timeout.

GDPR: Never log answer content. Log only tenant_id and categorical case identifiers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe
from pydantic import BaseModel, Field

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_DISCLAIMER_PATH = _PROMPTS_DIR / "disclaimer_medical_pl_v1.md"
_GUARDRAILS_PROMPT_PATH = _PROMPTS_DIR / "guardrails_output_v1.md"
_GUARDRAILS_PROMPT_V2_PATH = _PROMPTS_DIR / "guardrails_output_v2.md"

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
_guardrails_prompt_v2_cache: str | None = None


class GuardrailsDecision(BaseModel):
    """Structured output from the LLM safety evaluation pass.

    v1 fields: safe, reason_code (categorical — safe to log)
    v2 fields (ADR-017): modifications, disclaimer_added, pii_detected, reasoning
      reasoning is NEVER logged — it may contain answer-derived free text (GDPR).
    """

    safe: bool
    reason_code: str | None = None  # v1: categorical, safe to log
    modifications: list[str] = Field(default_factory=list)  # v2: safe textual fixes
    disclaimer_added: bool = False  # v2: LLM recommends/detects disclaimer
    pii_detected: bool = False  # v2: PII detected in answer
    reasoning: str | None = Field(default=None, exclude=True)  # v2: NEVER serialise/log


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


def _load_guardrails_prompt_v2() -> str:
    global _guardrails_prompt_v2_cache
    if _guardrails_prompt_v2_cache is None:
        _guardrails_prompt_v2_cache = _GUARDRAILS_PROMPT_V2_PATH.read_text(encoding="utf-8")
    return _guardrails_prompt_v2_cache


_GUARDRAILS_TIMEOUT_S = 30.0  # ADR-017 §4: reduced budget for guardrails LLM call


async def _run_llm_guardrails(
    answer: str,
    state: QueryState,
    config: dict[str, Any],
    *,
    use_v2: bool = False,
) -> GuardrailsDecision:
    """Call the LLM guardrails prompt and return a structured safety decision.

    Fail-open: any exception is re-raised so the caller can log and continue.

    Args:
        answer: The generated answer text to evaluate.
        state: Full pipeline state (used for model resolution and context chunks).
        config: RunnableConfig with configurable["llm"] and configurable["db"].
        use_v2: When True, use guardrails_output_v2.md with full ADR-017 schema.
                When False (default), use guardrails_output_v1.md (legacy).

    Returns:
        GuardrailsDecision with safe=True/False and structured metadata.

    Raises:
        Exception: Any LLM or parse failure — caller handles fail-open logic.
    """
    cfg = config.get("configurable", {})
    llm = cfg.get("llm")
    db = cfg.get("db")
    if llm is None or db is None:
        raise ValueError("configurable must contain 'llm' and 'db' for LLM guardrails")

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise ValueError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    if use_v2:
        # v2: inject both ANSWER and CONTEXT (graded chunks highlight_text only — no IDs)
        context_snippets = [
            c.get("highlight_text", "")
            for c in (state.graded_chunks or [])
            if c.get("highlight_text")
        ]
        context_str = "\n---\n".join(context_snippets) if context_snippets else "(no context)"
        prompt = (
            _load_guardrails_prompt_v2()
            .replace("{{ANSWER}}", answer)
            .replace("{{CONTEXT}}", context_str)
        )
    else:
        prompt = _load_guardrails_prompt().replace("{{ANSWER}}", answer)

    response = await llm.chat_completion(
        model=model_record.model_id,
        messages=[
            {"role": "system", "content": prompt},
        ],
        base_url=model_record.endpoint_url,
        response_format={"type": "json_object"},
        temperature=0.0,
        timeout=_GUARDRAILS_TIMEOUT_S,
    )
    raw = response.choices[0].message.content or ""
    parsed = json.loads(raw)

    if use_v2:
        return GuardrailsDecision(
            safe=parsed.get("safe", True),
            modifications=parsed.get("modifications", []),
            disclaimer_added=bool(parsed.get("disclaimer_added", False)),
            pii_detected=bool(parsed.get("pii_detected", False)),
            # reasoning intentionally NOT logged anywhere — assigned to model only
            reasoning=parsed.get("reasoning"),
        )
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
    #
    # llm_check (ADR-017 v2): structured output with factual grounding check,
    #   modifications, disclaimer_added, pii_detected, and reasoning (never logged).
    # llm_guardrails_enabled (legacy v1): safe/reason_code only.
    llm_check = bool(state.guardrails_config.get("llm_check", False))
    llm_enabled_legacy = bool(state.guardrails_config.get("llm_guardrails_enabled", False))
    llm_enabled = llm_check or llm_enabled_legacy

    if llm_enabled and case_applied in ("disclaimer_appended", "passthrough") and answer:
        try:
            decision = await _run_llm_guardrails(answer, state, config, use_v2=llm_check)
            if not decision.safe:
                answer = _LLM_REFUSAL_ANSWER
                llm_guardrails_triggered = True
                logger.warning(
                    "node_guardrails_output.llm_guardrails_blocked",
                    tenant_id=str(state.tenant_id),
                    # reason_code is categorical — safe to log; reasoning is NEVER logged
                    reason_code=decision.reason_code,
                    pii_detected=decision.pii_detected,
                )
            elif llm_check:
                # Apply safe modifications suggested by the LLM (v2 only).
                # Length guard: discard modification if it is implausibly long (>3× original)
                # — prevents prompt injection amplification via crafted modifications.
                if decision.modifications:
                    candidate = decision.modifications[0]
                    if len(candidate) <= 3 * max(len(answer), 1):
                        # Re-append disclaimer if the rule-based pass had already added it,
                        # so the LLM modification does not silently drop the disclaimer text.
                        if case_applied == "disclaimer_appended":
                            disclaimer = _load_disclaimer()
                            answer = f"{candidate}\n\n{disclaimer}"
                        else:
                            answer = candidate
                    else:
                        logger.warning(
                            "node_guardrails_output.modification_discarded_too_long",
                            tenant_id=str(state.tenant_id),
                        )
                # If LLM recommends a disclaimer and rule-based pass didn't add one, add now
                if decision.disclaimer_added and case_applied == "passthrough":
                    disclaimer = _load_disclaimer()
                    answer = f"{answer}\n\n{disclaimer}"
                    case_applied = "disclaimer_appended"
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
            "llm_check": llm_check,
            "llm_guardrails_enabled": llm_enabled,
            "llm_guardrails_triggered": llm_guardrails_triggered,
            # reasoning is intentionally absent — GDPR
        }
    )
    logger.info(
        "node_guardrails_output.completed",
        tenant_id=str(state.tenant_id),
        case=case_applied,
        llm_check=llm_check,
        llm_guardrails_enabled=llm_enabled,
        llm_guardrails_triggered=llm_guardrails_triggered,
    )
    return {"answer": answer}
