"""node_translate_query — translate query to collection's primary language via LLM.

Only runs when detected_language != collection.primary_language (conditional edge).
Sets state.translated_query and state.cross_language_retrieval = True.

Prompt: src/graphs/prompts/query_translation_v1.md (versioned, immutable).

GDPR:
- Query text is never logged — only language codes and latency.
- Translated query is never logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}

_LANG_NAMES: dict[str, str] = {
    "pol": "Polish",
    "eng": "English",
    "deu": "German",
    "fra": "French",
    "spa": "Spanish",
    "ita": "Italian",
    "ukr": "Ukrainian",
    "rus": "Russian",
}


def _load_prompt(version: str = "v1") -> str:
    if version not in _PROMPT_CACHE:
        path = _PROMPTS_DIR / f"query_translation_{version}.md"
        _PROMPT_CACHE[version] = path.read_text(encoding="utf-8")
    return _PROMPT_CACHE[version]


def _lang_name(code: str) -> str:
    return _LANG_NAMES.get(code, code)


@observe(name="node_translate_query", capture_input=False, capture_output=False)
async def node_translate_query(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Translate the rewritten query to the collection's primary language.

    Args:
        state: Must have rewritten_query, detected_language, collection_ids, llm_model_id.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        {"translated_query": str, "cross_language_retrieval": True}

    Raises:
        QueryNodeError: If LLM translation fails.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    llm = cfg["llm"]
    node_start = datetime.now(UTC)

    query_text = state.rewritten_query or state.question
    source_lang = state.detected_language or "pol"

    # Resolve target language from collection primary_language
    from sqlalchemy import select

    from src.db.models.collection import Collection
    from src.db.models.models_registry import ModelsRegistry

    target_lang = "pol"  # default
    if state.collection_ids:
        col_result = await db.execute(
            select(Collection).where(Collection.id == state.collection_ids[0])
        )
        col = col_result.scalar_one_or_none()
        if col is not None:
            target_lang = getattr(col, "primary_language", "pol") or "pol"

        # Resolve LLM model
        model_result = await db.execute(
            select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id)
        )
        model_record = model_result.scalar_one_or_none()
        if model_record is None:
            raise QueryNodeError(f"LLM model not found: {state.llm_model_id}")
    else:
        raise QueryNodeError("No collection_ids to resolve translation target language")

    # Build prompt
    prompt_template = _load_prompt("v1")
    prompt = (
        prompt_template.replace("{source_lang}", _lang_name(source_lang))
        .replace("{target_lang}", _lang_name(target_lang))
        .replace("{query}", query_text)
    )

    try:
        response = await llm.chat_completion(
            model=model_record.model_id,
            messages=[{"role": "user", "content": prompt}],
            base_url=model_record.endpoint_url,
            temperature=0.0,
        )
        translated = (response.choices[0].message.content or "").strip()
        if not translated:
            translated = query_text  # fallback: use original
    except Exception as exc:
        raise QueryNodeError(f"query_translation_error: {type(exc).__name__}") from exc

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_translate_query.completed",
        tenant_id=str(state.tenant_id),
        source_lang=source_lang,
        target_lang=target_lang,
        latency_ms=elapsed_ms,
    )

    return {
        "translated_query": translated,
        "cross_language_retrieval": True,
    }
