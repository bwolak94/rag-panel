"""node_grade_documents — grade retrieved chunks for relevance to the user question.

All chunks are graded in a single LLM call for performance.
Chunks scoring relevant=false are filtered out.
If no chunks pass grading, no_results=True is set so guardrails handles the response.

GDPR: Never log chunk text or question content.
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

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "grade_documents_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _build_batch_prompt(question: str, chunks: list[dict[str, Any]]) -> str:
    """Build a single prompt asking LLM to grade all chunks at once.

    Returns a modified system prompt that requests a JSON array of
    {chunk_index, relevant} decisions instead of a single-chunk evaluation.
    """
    base_prompt = _load_prompt()

    # Build numbered chunk list
    chunks_text = "\n\n".join(
        f'<chunk index="{i + 1}">{chunk.get("highlight_text", "")}</chunk>'
        for i, chunk in enumerate(chunks)
    )

    batch_instruction = (
        "\n\n## Batch grading mode\n\n"
        "You are grading MULTIPLE chunks at once.\n"
        "Return ONLY a valid JSON object with a 'grades' array:\n"
        '{"grades": [{"chunk_index": <1-based integer>, "relevant": <true|false>}]}\n\n'
        "Grade ALL chunks listed below. Include every chunk_index in the response.\n\n"
        f"<QUESTION>\n{question}\n</QUESTION>\n\n"
        "<ALL_CHUNKS>\n"
        f"{chunks_text}\n"
        "</ALL_CHUNKS>"
    )

    # Replace the single-chunk template section with batch instruction
    # Strip the original input section and append batch version
    prompt_parts = base_prompt.split("## Input")
    system_part = prompt_parts[0].strip() if prompt_parts else base_prompt
    return system_part + batch_instruction


@observe(capture_input=False, capture_output=False)
async def node_grade_documents(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Grade retrieved chunks for relevance. Filter out irrelevant ones.

    Args:
        state: Must have retrieved_chunks, question, llm_model_id.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        {"graded_chunks": list[dict], "no_results": bool}

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]

    chunks = state.retrieved_chunks
    if not chunks:
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "model_id": str(state.llm_model_id),
                "relevant_count": 0,
                "total_count": 0,
            }
        )
        logger.info(
            "node_grade_documents.no_chunks_to_grade",
            tenant_id=str(state.tenant_id),
        )
        return {"graded_chunks": [], "no_results": True}

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    prompt = _build_batch_prompt(state.question, chunks)

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
        grades: list[dict[str, Any]] = parsed.get("grades", [])
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError):
        logger.warning(
            "node_grade_documents.parse_error_keep_all",
            tenant_id=str(state.tenant_id),
            chunk_count=len(chunks),
        )
        # Fallback: keep all chunks if grading fails
        return {"graded_chunks": chunks, "no_results": False}
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"grade_documents_llm_error: {type(exc).__name__}") from exc

    # Build set of relevant chunk indices (1-based)
    relevant_indices: set[int] = set()
    for grade in grades:
        if grade.get("relevant") is True:
            idx = grade.get("chunk_index")
            if isinstance(idx, int):
                relevant_indices.add(idx)

    graded_chunks = [chunk for i, chunk in enumerate(chunks, start=1) if i in relevant_indices]
    no_results = len(graded_chunks) == 0

    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "model_id": str(model_record.id),
            "total_count": len(chunks),
            "relevant_count": len(graded_chunks),
            "no_results": no_results,
        }
    )
    logger.info(
        "node_grade_documents.completed",
        tenant_id=str(state.tenant_id),
        retrieved=len(chunks),
        graded=len(graded_chunks),
        no_results=no_results,
    )
    return {"graded_chunks": graded_chunks, "no_results": no_results}
