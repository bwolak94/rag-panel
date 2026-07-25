"""node_rewrite_query — rewrite the user query for optimal dense retrieval.

Expands abbreviations, adds synonyms, resolves coreferences from conversation history.
Falls back to original question on any parsing failure.

GDPR: Never log question or rewritten query content — only node name, tenant_id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from src.core.exceptions import QueryNodeError
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "rewrite_query_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _format_history(history: list[dict[str, str]]) -> str:
    """Format conversation history as 'role: content' lines."""
    if not history:
        return "(brak historii)"
    return "\n".join(f"{entry['role']}: {entry['content']}" for entry in history)


async def node_rewrite_query(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Rewrite query for better embedding retrieval.

    Args:
        state: Must have question, llm_model_id, conversation_history set.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        {"rewritten_query": str}

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(
        select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id)
    )
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    history_text = _format_history(state.conversation_history)
    prompt = (
        _load_prompt()
        .replace("{{USER_QUESTION}}", state.question)
        .replace("{{CONVERSATION_HISTORY}}", history_text)
    )

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
        rewritten_query = parsed.get("rewritten_query", "").strip()
        if not rewritten_query:
            rewritten_query = state.question
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        logger.warning(
            "node_rewrite_query.parse_error_fallback",
            tenant_id=str(state.tenant_id),
        )
        rewritten_query = state.question
    except QueryNodeError:
        raise
    except Exception as exc:
        logger.warning(
            "node_rewrite_query.llm_error_fallback",
            tenant_id=str(state.tenant_id),
            error_type=type(exc).__name__,
        )
        # Fallback to original question rather than failing the whole pipeline
        rewritten_query = state.question

    logger.info(
        "node_rewrite_query.completed",
        tenant_id=str(state.tenant_id),
    )
    return {"rewritten_query": rewritten_query}
