"""node_classify_intent — classify user query intent before retrieval.

Calls the LLM to classify intent into: topical | chitchat | out_of_scope.
Prompt injections from user input are mitigated via explicit delimiters in the prompt.

GDPR: Never log question content — only intent and node name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "classify_intent_v1.md"
_VALID_INTENTS = frozenset({"topical", "chitchat", "out_of_scope"})
_DEFAULT_INTENT = "topical"  # Conservative: attempt retrieval rather than suppress a valid query

# Module-level cache: read once to avoid repeated I/O
_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


@observe(capture_input=False, capture_output=False)
async def node_classify_intent(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Classify the user's question intent.

    Args:
        state: Must have question set.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        {"intent": str, "halt": bool}

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    # Resolve LLM model from DB
    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    prompt = _load_prompt().replace("{{USER_QUESTION}}", state.question)

    try:
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
        intent = parsed.get("intent", _DEFAULT_INTENT)
        if intent not in _VALID_INTENTS:
            logger.warning(
                "node_classify_intent.unexpected_intent",
                tenant_id=str(state.tenant_id),
                received_intent=intent,
            )
            intent = _DEFAULT_INTENT
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        logger.warning(
            "node_classify_intent.parse_error",
            tenant_id=str(state.tenant_id),
        )
        intent = _DEFAULT_INTENT
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"classify_intent_llm_error: {type(exc).__name__}") from exc

    halt = intent != "topical"
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "intent": intent,
            "halt": halt,
        }
    )
    logger.info(
        "node_classify_intent.completed",
        tenant_id=str(state.tenant_id),
        intent=intent,
        halt=halt,
    )
    return {"intent": intent, "halt": halt}
