"""node_research_plan — generate the next sub-query and decide if evidence is sufficient.

Given the original question and the results accumulated in previous iterations, the LLM
produces a JSON object describing:
  - the next focused sub-query to retrieve,
  - its reasoning about what evidence was found and what is still missing,
  - a boolean flag indicating whether the existing evidence is sufficient to answer.

The node appends a new ResearchIteration (with sufficient / reasoning set, but with an
empty retrieved_chunks list — node_research_retrieve fills that in next).

GDPR:
- Never log question text, sub-query text, chunk content, or reasoning strings.
- Reasoning strings are LLM-generated and re-injected into subsequent prompts as
  planning context — they are not logged and not stored beyond the graph run.
- Log only tenant_id, pipeline_id, step number, sufficient flag, latency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from langfuse import observe

from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.graphs.research_graph.state import ResearchIteration, ResearchState

logger = structlog.get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "research_plan_v1.md"

_prompt_cache: str | None = None


def _load_prompt() -> str:
    global _prompt_cache
    if _prompt_cache is None:
        _prompt_cache = _PROMPT_PATH.read_text(encoding="utf-8")
    return _prompt_cache


def _format_previous_iterations(iterations: list[ResearchIteration]) -> str:
    """Render all completed iteration summaries for injection into the planning prompt.

    Metadata (step number, sufficient flag, chunk count), the sub-query, and the
    LLM-generated reasoning string are included so the planner can avoid repeating
    previous searches.  Raw chunk content is never included here.

    Note: reasoning strings are LLM-generated (not user input) and must NOT be
    logged to application logs — they are only passed back into the prompt as
    context for the next planning step.
    """
    if not iterations:
        return "(nenhuma iteração anterior)"
    parts: list[str] = []
    for it in iterations:
        parts.append(
            f'<iteration step="{it.step}" sufficient="{str(it.sufficient).lower()}" '
            f'chunks_retrieved="{len(it.retrieved_chunks)}">'
            f"<sub_query>{it.query}</sub_query>"
            f"<reasoning>{it.reasoning}</reasoning>"
            f"</iteration>"
        )
    return "\n".join(parts)


@observe(capture_input=False, capture_output=False)
async def node_research_plan(state: ResearchState, config: dict[str, Any]) -> dict[str, Any]:
    """Plan the next research iteration or declare sufficiency.

    Calls the LLM with the original question and a summary of previous iterations.
    Parses the JSON response: {"sub_query": "...", "reasoning": "...", "sufficient": bool}.
    Appends a new ResearchIteration to state.iterations with retrieved_chunks=[] (filled
    later by node_research_retrieve) and sets steps_taken.

    Args:
        state: Must have original_question, iterations, llm_model_id. The DB must have
               a matching ModelsRegistry record for the LLM.
        config: RunnableConfig with configurable["db"] and configurable["llm"].

    Returns:
        dict with keys: iterations (updated list), steps_taken.

    Raises:
        QueryNodeError: On LLM failure, JSON parse error, or missing model record.
    """
    cfg = config.get("configurable", {})
    db = cfg["db"]
    llm = cfg["llm"]

    node_start = datetime.now(UTC)
    current_step = len(state.iterations) + 1

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(
            f"research_plan: LLM model not found: llm_model_id={state.llm_model_id}"
        )

    previous_iterations_text = _format_previous_iterations(state.iterations)

    prompt = (
        _load_prompt()
        .replace("{{ORIGINAL_QUESTION}}", state.original_question)
        .replace("{{PREVIOUS_ITERATIONS}}", previous_iterations_text)
        .replace("{{CURRENT_STEP}}", str(current_step))
        .replace("{{MAX_STEPS}}", str(state.max_steps))
    )

    try:
        response = await llm.chat_completion(
            model=model_record.model_id,
            messages=[{"role": "system", "content": prompt}],
            base_url=model_record.endpoint_url,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(raw)
        sub_query: str = parsed.get("sub_query", "")
        reasoning: str = parsed.get("reasoning", "")
        sufficient: bool = bool(parsed.get("sufficient", False))
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        raise QueryNodeError(f"research_plan_parse_error: {type(exc).__name__}") from exc
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"research_plan_llm_error: {type(exc).__name__}") from exc

    new_iteration = ResearchIteration(
        step=current_step,
        query=sub_query,
        retrieved_chunks=[],  # filled by node_research_retrieve
        reasoning=reasoning,
        sufficient=sufficient,
    )
    updated_iterations = list(state.iterations) + [new_iteration]

    elapsed_ms = int((datetime.now(UTC) - node_start).total_seconds() * 1000)
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "pipeline_id": str(state.pipeline_id),
            "step": current_step,
            "sufficient": sufficient,
            "model_id": str(model_record.id),
            "latency_ms": elapsed_ms,
        }
    )
    logger.info(
        "node_research_plan.completed",
        tenant_id=str(state.tenant_id),
        pipeline_id=str(state.pipeline_id),
        step=current_step,
        sufficient=sufficient,
        latency_ms=elapsed_ms,
    )

    return {
        "iterations": updated_iterations,
        "steps_taken": current_step,
    }
