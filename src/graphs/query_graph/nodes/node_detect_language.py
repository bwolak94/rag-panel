"""node_detect_language — detect query language using langdetect.

Fast, CPU-only detection (no LLM call). Runs after node_rewrite_query,
before node_translate_query (conditional) and node_retrieve.

GDPR: query text is never logged. Only the detected language code is logged.
"""

from __future__ import annotations

from typing import Any

import structlog
from langfuse import observe

from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_DEFAULT_LANGUAGE = "pol"
_CONFIDENCE_THRESHOLD = 0.8

# Map langdetect ISO 639-1 → ISO 639-3
_LANG_MAP: dict[str, str] = {
    "pl": "pol",
    "en": "eng",
    "de": "deu",
    "fr": "fra",
    "es": "spa",
    "it": "ita",
    "uk": "ukr",
    "ru": "rus",
}


def _normalize_lang(code: str) -> str:
    """Convert ISO 639-1 to ISO 639-3. Returns code unchanged if not in map."""
    return _LANG_MAP.get(code.lower(), code.lower())


@observe(name="node_detect_language", capture_input=False, capture_output=False)
async def node_detect_language(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Detect the language of the rewritten query.

    Uses langdetect with a confidence threshold. Falls back to "pol" when
    detection fails or confidence is below threshold.

    Args:
        state: Must have rewritten_query or question.
        config: RunnableConfig (unused by this node).

    Returns:
        {"detected_language": str, "response_language": str}
    """
    query_text = state.rewritten_query or state.question

    # Resolve response_language from pipeline config:
    #   "auto" → use detected language
    #   "pol" / "eng" / ... → forced language
    pipeline_response_lang: str = (state.prompt_config or {}).get("response_language", "auto")

    detected = _DEFAULT_LANGUAGE
    try:
        from langdetect import detect_langs

        results = detect_langs(query_text)
        if results and results[0].prob >= _CONFIDENCE_THRESHOLD:
            detected = _normalize_lang(results[0].lang)
        else:
            logger.info(
                "node_detect_language.low_confidence",
                tenant_id=str(state.tenant_id),
                fallback=_DEFAULT_LANGUAGE,
            )
    except Exception:
        logger.info(
            "node_detect_language.detection_failed",
            tenant_id=str(state.tenant_id),
            fallback=_DEFAULT_LANGUAGE,
        )

    response_language = detected if pipeline_response_lang == "auto" else pipeline_response_lang

    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "detected_language": detected,
            "response_language": response_language,
        }
    )
    logger.info(
        "node_detect_language.completed",
        tenant_id=str(state.tenant_id),
        detected_language=detected,
        response_language=response_language,
    )

    return {
        "detected_language": detected,
        "response_language": response_language,
    }
