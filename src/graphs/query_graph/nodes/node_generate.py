"""node_generate — generate the final answer using graded context chunks.

Calls the LLM with graded chunks as context. Parses JSON response with
"answer" and "citations" array. Maps citations to MessageSourceOut format.

Before building the context string the node applies a token budget: chunks
are sorted by score (descending) and the lowest-scoring chunks are dropped
until the total context token count fits within the budget.  The budget is
resolved from (in priority order):
  1. state.context_token_budget  (set by the caller / runtime override)
  2. state.prompt_config["max_context_tokens"]  (per-pipeline prompt config — ADR-021)
  3. state.guardrails_config["context_token_budget"]  (legacy per-pipeline override)
  4. Settings.DEFAULT_CONTEXT_TOKEN_BUDGET  (global fallback, default 6 000)
When all of the above are absent, the global fallback is used.

Early stopping (ADR-021): if ALL chunks are dropped by the budget (budget too
small to fit even the highest-scoring chunk), the node returns
``{"no_results": True}`` without calling the LLM, preventing an empty-context
generation that could hallucinate.

AB testing (shadow mode)
------------------------
When ``state.prompt_config`` contains a valid ``ab_test`` block with
``enabled=true`` and the traffic-split coin-flip succeeds,
``domain.ab_testing_service.should_run_shadow()`` returns ``True`` and
``domain.ab_testing_service.schedule_shadow_task()`` launches a background
``asyncio.Task`` that runs the shadow prompt after the main answer has already
been produced.  The user response is never delayed by this.

GDPR:
- Never log question, answer, or chunk text content.
- Log only tenant_id, token counts, chunk count, latency.
- AB-test shadow task stores only answer lengths and latencies — never text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import tiktoken
from langfuse import observe

from src.core.config import settings
from src.core.exceptions import QueryNodeError
from src.core.langfuse_client import update_span_metadata as _lf_update_span
from src.domain.ab_testing_service import schedule_shadow_task, should_run_shadow
from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"

_prompt_cache: dict[str, str] = {}


def _load_prompt(version: str = "v1") -> str:
    """Load and cache a versioned generate prompt from disk.

    Args:
        version: Prompt file version suffix, e.g. ``"v1"`` or ``"v2"``.
            The file ``src/graphs/prompts/generate_<version>.md`` must exist.

    Returns:
        The prompt file contents as a string.
    """
    if version not in _prompt_cache:
        path = _PROMPTS_DIR / f"generate_{version}.md"
        _prompt_cache[version] = path.read_text(encoding="utf-8")
    return _prompt_cache[version]


def _count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Return the number of tokens in *text* using the specified tiktoken encoding.

    Args:
        text: Plain text to tokenise.
        encoding_name: tiktoken encoding name; defaults to cl100k_base which
            covers GPT-3.5/4 and is a reasonable approximation for other models.

    Returns:
        Integer token count.
    """
    enc = tiktoken.get_encoding(encoding_name)
    return len(enc.encode(text))


def _trim_chunks_to_budget(
    chunks: list[dict[str, Any]], budget: int
) -> tuple[list[dict[str, Any]], int, int]:
    """Keep the highest-scoring chunks whose combined context tokens fit *budget*.

    Chunks are sorted by ``score`` descending so that the most relevant context
    is always retained.  Lower-scoring chunks are dropped when the running token
    total would exceed the budget.

    Args:
        chunks: Graded chunks list (each dict must have ``highlight_text`` and
            optionally ``score``).
        budget: Maximum total token count for the assembled context.

    Returns:
        A tuple of ``(kept_chunks, dropped_count, tokens_used)`` where *kept_chunks*
        preserves the original list order (score-sorted order is used only for the
        selection decision) and *tokens_used* is the total token count of the kept chunks.
    """
    if not chunks:
        return chunks, 0, 0

    # Sort a copy by score descending; keep original index for stable re-sort
    scored = sorted(enumerate(chunks), key=lambda t: t[1].get("score", 0.0), reverse=True)

    kept_indices: set[int] = set()
    running_tokens = 0

    for original_idx, chunk in scored:
        text = chunk.get("highlight_text", "")
        token_count = _count_tokens(text)
        if running_tokens + token_count <= budget:
            kept_indices.add(original_idx)
            running_tokens += token_count

    # Rebuild list in original order
    kept = [c for i, c in enumerate(chunks) if i in kept_indices]
    dropped = len(chunks) - len(kept)
    return kept, dropped, running_tokens


def _format_history(history: list[dict[str, str]]) -> str:
    if not history:
        return "(brak historii)"
    return "\n".join(f"{entry['role']}: {entry['content']}" for entry in history)


def _build_context_chunks(chunks: list[dict[str, Any]]) -> str:
    """Format graded chunks with XML delimiters for the prompt."""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        doc_id = chunk.get("document_id", "unknown")
        page = chunk.get("page_number", "")
        text = chunk.get("highlight_text", "")
        parts.append(f'<chunk index="{i}" document_id="{doc_id}" page="{page}">{text}</chunk>')
    return "\n\n".join(parts)


def _build_graph_context_block(graph_context: list[dict[str, Any]]) -> str:
    """Render knowledge-graph entities and relations as an XML block for the prompt.

    Only icd_code and atc_code attributes are included when they are not None.
    Entity names and relation names are treated as untrusted data (document-derived) —
    they appear inside the XML value attributes, never in log output.

    Args:
        graph_context: List of entity dicts produced by node_graph_retrieve.
            Each dict has keys: entity, entity_type, icd_code, atc_code, related.
            ``related`` is a list of dicts with keys: name, relation, direction,
            confidence.

    Returns:
        A ``<graph_context>...</graph_context>`` XML string, or an empty string
        when *graph_context* is falsy.
    """
    if not graph_context:
        return ""

    lines: list[str] = ["<graph_context>"]
    for entry in graph_context:
        entity_name = entry.get("entity", "")
        entity_type = entry.get("entity_type", "")
        icd_code: str | None = entry.get("icd_code")
        atc_code: str | None = entry.get("atc_code")

        # Build opening tag — only include optional code attributes when present
        attrs = f'name="{entity_name}" type="{entity_type}"'
        if icd_code is not None:
            attrs += f' icd_code="{icd_code}"'
        if atc_code is not None:
            attrs += f' atc_code="{atc_code}"'
        lines.append(f"  <entity {attrs}>")

        for rel in entry.get("related", []):
            rel_name = rel.get("name", "")
            rel_type = rel.get("relation", "")
            direction = rel.get("direction", "")
            confidence = rel.get("confidence", 0.0)
            lines.append(
                f'    <related name="{rel_name}" relation="{rel_type}"'
                f' direction="{direction}" confidence="{confidence:.2f}"/>'
            )

        lines.append("  </entity>")
    lines.append("</graph_context>")
    return "\n".join(lines)


def _map_citation_to_source(
    citation: dict[str, Any], chunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """Map an LLM citation (chunk_index) to a MessageSourceOut-compatible dict.

    Extracts document_id, collection_id, page_number, highlight_text
    from the chunk payload at the cited index.
    """
    chunk_index = citation.get("chunk_index")
    if not isinstance(chunk_index, int) or chunk_index < 1 or chunk_index > len(chunks):
        return {}

    chunk = chunks[chunk_index - 1]
    payload = chunk.get("payload", {})

    return {
        "document_id": chunk.get("document_id", ""),
        "collection_id": chunk.get("collection_id") or payload.get("collection_id", ""),
        "document_title": payload.get("document_title") or payload.get("filename", ""),
        "section_heading": payload.get("section") or payload.get("heading"),
        "source_url": payload.get("source_url"),
        "chunk_id": str(chunk.get("chunk_id")) if chunk.get("chunk_id") else None,
        "page_number": chunk.get("page_number"),
        "highlight_text": chunk.get("highlight_text"),
        "relevance_score": chunk.get("score", 0.0),
    }


def _resolve_token_budget(state: QueryState) -> int:
    """Return the effective context token budget for this request.

    Resolution order (first non-None value wins):
    1. ``state.context_token_budget`` — explicit per-request runtime override.
    2. ``state.prompt_config["max_context_tokens"]`` — per-pipeline prompt config
       (ADR-021; set via ``PromptConfig.max_context_tokens``).
    3. ``state.guardrails_config["context_token_budget"]`` — legacy per-pipeline
       value in ``RagPipeline.guardrails_config`` (kept for backwards compatibility).
    4. ``settings.DEFAULT_CONTEXT_TOKEN_BUDGET`` — global application default.
    """
    if state.context_token_budget is not None:
        return state.context_token_budget

    prompt_budget = state.prompt_config.get("max_context_tokens")
    if prompt_budget is not None:
        return int(prompt_budget)

    pipeline_budget = state.guardrails_config.get("context_token_budget")
    if pipeline_budget is not None:
        return int(pipeline_budget)

    return settings.DEFAULT_CONTEXT_TOKEN_BUDGET


@observe(as_type="generation", capture_input=False, capture_output=False)
async def node_generate(state: QueryState, config: dict[str, Any]) -> dict[str, Any]:
    """Generate the final RAG answer with citations.

    Args:
        state: Must have graded_chunks, question, llm_model_id, conversation_history.
            Optional: context_token_budget, guardrails_config.
        config: RunnableConfig with configurable["llm"] and configurable["db"].

    Returns:
        dict with keys: answer, citations, prompt_tokens, completion_tokens,
        chunks_trimmed, context_tokens_used.
        When all chunks are dropped by the budget, returns ``{"no_results": True}``
        without calling the LLM (ADR-021 early stopping).

    Raises:
        QueryNodeError: If LLM call fails fatally.
    """
    cfg = config.get("configurable", {})
    llm = cfg["llm"]
    db = cfg["db"]
    # session_factory is optional; only required when AB testing is active.
    session_factory = cfg.get("session_factory")

    node_start = datetime.now(UTC)

    from sqlalchemy import select

    from src.db.models.models_registry import ModelsRegistry

    result = await db.execute(select(ModelsRegistry).where(ModelsRegistry.id == state.llm_model_id))
    model_record = result.scalar_one_or_none()
    if model_record is None:
        raise QueryNodeError(f"LLM model not found: llm_model_id={state.llm_model_id}")

    # --- Resolve prompt version and language before budget calculation ---
    # These are needed to render the prompt overhead (system prompt + question + history)
    # so we can subtract it from the total budget before trimming chunks (ADR-021 §2b).
    control_version: str = state.prompt_config.get("generate_prompt_version", "v1")
    history_text = _format_history(state.conversation_history)
    _lang_names_full: dict[str, str] = {
        "pol": "Polish",
        "eng": "English",
        "deu": "German",
        "fra": "French",
        "spa": "Spanish",
    }
    response_lang_name = _lang_names_full.get(state.response_language, state.response_language)
    prompt_template = _load_prompt(control_version)

    # Compute overhead tokens: prompt rendered with question + history but EMPTY context.
    # This is the fixed cost of the prompt that cannot be trimmed.
    prompt_overhead = (
        prompt_template
        .replace("{{QUESTION}}", state.question)
        .replace("{{CONTEXT_CHUNKS}}", "")
        .replace("{{CONVERSATION_HISTORY}}", history_text)
        .replace("{{RESPONSE_LANGUAGE}}", response_lang_name)
    )
    overhead_tokens = _count_tokens(prompt_overhead)

    # --- Token budget: trim low-score chunks to the remaining budget ---
    budget = _resolve_token_budget(state)
    chunk_budget = max(0, budget - overhead_tokens)
    graded_chunks, chunks_dropped, context_tokens_used = _trim_chunks_to_budget(
        state.graded_chunks, chunk_budget
    )

    if chunks_dropped > 0:
        logger.warning(
            "node_generate.context_trimmed",
            tenant_id=str(state.tenant_id),
            pipeline_id=str(state.pipeline_id),
            chunks_before=len(state.graded_chunks),
            chunks_dropped=chunks_dropped,
            chunk_budget=chunk_budget,
            overhead_tokens=overhead_tokens,
            total_budget=budget,
        )

    # Early stopping (ADR-021): if all chunks were dropped by the budget, routing
    # to the "not found" path prevents empty-context generation that could hallucinate.
    if state.graded_chunks and not graded_chunks:
        logger.warning(
            "node_generate.early_stop_zero_chunks",
            tenant_id=str(state.tenant_id),
            pipeline_id=str(state.pipeline_id),
            chunks_before=len(state.graded_chunks),
            chunk_budget=chunk_budget,
            overhead_tokens=overhead_tokens,
            total_budget=budget,
        )
        _lf_update_span(
            metadata={
                "tenant_id": str(state.tenant_id),
                "early_stop": True,
                "chunks_before": len(state.graded_chunks),
                "overhead_tokens": overhead_tokens,
                "total_budget": budget,
                "chunk_budget": chunk_budget,
            }
        )
        return {
            "no_results": True,
            "chunks_trimmed": chunks_dropped,
            "chunks_included": 0,
            "context_tokens_used": 0,
        }

    context_chunks_text = _build_context_chunks(graded_chunks)
    prompt = (
        prompt_template
        .replace("{{QUESTION}}", state.question)
        .replace("{{CONTEXT_CHUNKS}}", context_chunks_text)
        .replace("{{CONVERSATION_HISTORY}}", history_text)
        .replace("{{RESPONSE_LANGUAGE}}", response_lang_name)
    )

    # Inject graph context block immediately before <CONTEXT> when available.
    # graph_context is set by node_graph_retrieve (opt-in via graph_rag_enabled).
    # Log only the count — never log entity names (GDPR).
    if state.graph_context:
        graph_block = _build_graph_context_block(state.graph_context)
        prompt = prompt.replace("<CONTEXT>", f"{graph_block}\n\n<CONTEXT>")
        logger.debug(
            "node_generate.graph_context_injected",
            tenant_id=str(state.tenant_id),
            entity_count=len(state.graph_context),
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
        answer: str = parsed.get("answer", "")
        llm_citations: list[dict[str, Any]] = parsed.get("citations", [])
    except (json.JSONDecodeError, KeyError, IndexError, AttributeError) as exc:
        raise QueryNodeError(f"generate_parse_error: {type(exc).__name__}") from exc
    except QueryNodeError:
        raise
    except Exception as exc:
        raise QueryNodeError(f"generate_llm_error: {type(exc).__name__}") from exc

    # Extract token counts from response
    prompt_tokens = 0
    completion_tokens = 0
    if hasattr(response, "usage") and response.usage is not None:
        prompt_tokens = response.usage.prompt_tokens or 0
        completion_tokens = response.usage.completion_tokens or 0

    # Map LLM citations to source dicts (use trimmed chunk list so indices align)
    citations: list[dict[str, Any]] = []
    for cit in llm_citations:
        mapped = _map_citation_to_source(cit, graded_chunks)
        if mapped:
            citations.append(mapped)

    elapsed_ms = (datetime.now(UTC) - node_start).total_seconds() * 1000.0
    _lf_update_span(
        metadata={
            "tenant_id": str(state.tenant_id),
            "model_id": str(model_record.id),
            "num_chunks_in": len(state.graded_chunks),
            "num_chunks_used": len(graded_chunks),
            "chunks_trimmed": chunks_dropped,
            "context_tokens_used": context_tokens_used,
            "overhead_tokens": overhead_tokens,
            "total_budget": budget,
            "chunk_budget": chunk_budget,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "citation_count": len(citations),
            "latency_ms": int(elapsed_ms),
        }
    )
    logger.info(
        "node_generate.completed",
        tenant_id=str(state.tenant_id),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        citation_count=len(citations),
        chunks_trimmed=chunks_dropped,
        chunks_included=len(graded_chunks),
        context_tokens_used=context_tokens_used,
        latency_ms=int(elapsed_ms),
    )

    # ------------------------------------------------------------------
    # AB testing — fire-and-forget shadow call (never delays response)
    # ------------------------------------------------------------------
    ab_config: dict[str, Any] = state.prompt_config.get("ab_test", {})
    if ab_config and session_factory is not None and should_run_shadow(state.prompt_config):
        schedule_shadow_task(
            state=state,
            ab_config=ab_config,
            control_version=control_version,
            control_answer_length=len(answer),
            latency_control_ms=elapsed_ms,
            llm_client=llm,
            model_record=model_record,
            session_factory=session_factory,
        )

    return {
        "answer": answer,
        "citations": citations,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "chunks_trimmed": chunks_dropped,
        "chunks_included": len(graded_chunks),
        "context_tokens_used": context_tokens_used,
    }
