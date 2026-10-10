"""PromptABTestingService — shadow prompt execution and result recording.

Architecture
------------
When a pipeline has ``prompt_config.ab_test.enabled = true`` and the current
request is selected by the traffic-split coin-flip, a second "shadow" LLM call
is made with an alternate prompt version.  The shadow call is launched as a
background asyncio task (fire-and-forget) inside ``node_generate`` so the user
response is never delayed.

GDPR rules (medical data)
--------------------------
- Answer text is NEVER stored or logged.
- Question text is NEVER stored or logged.
- Only answer *lengths* (character counts) and *latencies* are persisted.
- Errors in the shadow call are caught and logged with identifiers only.

Public interface
----------------
``PromptABTestingService`` is instantiated with an ``AsyncSession`` so results
can be persisted to ``ab_test_results``.  The session is **not** committed here
— callers that need commit semantics (background tasks) must commit after
``record_result``.  When called from a fire-and-forget task, the service creates
its own session via the provided ``session_factory``.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.ab_test_result import ABTestResult as ABTestResultModel

if TYPE_CHECKING:
    from src.graphs.query_graph.state import QueryState

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "graphs" / "prompts"

# Module-level cache: version string → prompt text
_prompt_cache: dict[str, str] = {}


def _load_prompt_version(version: str) -> str:
    """Load and cache a versioned generate prompt from disk.

    Args:
        version: Prompt version string, e.g. ``"v2"``.  The file
            ``src/graphs/prompts/generate_<version>.md`` must exist.

    Returns:
        The prompt text as a string.

    Raises:
        FileNotFoundError: If no matching prompt file exists.
    """
    if version not in _prompt_cache:
        path = _PROMPTS_DIR / f"generate_{version}.md"
        _prompt_cache[version] = path.read_text(encoding="utf-8")
    return _prompt_cache[version]


class ABTestResultDTO(BaseModel):
    """Data-transfer object for one AB-test comparison result.

    Only non-content metrics are carried.  Answer text is never included.
    """

    experiment_id: str
    pipeline_id: UUID
    conversation_id: UUID
    tenant_id: UUID
    control_prompt_version: str
    shadow_prompt_version: str
    control_answer_length: int  # character count — not answer text
    shadow_answer_length: int  # character count — not answer text
    latency_control_ms: float
    latency_shadow_ms: float
    timestamp: datetime


def should_run_shadow(pipeline_config: dict[str, Any]) -> bool:
    """Decide whether a shadow call should run for this request.

    Stateless Bernoulli coin-flip — no DB session required.  Checks
    ``ab_test.enabled`` and flips against ``ab_test.traffic_split``.

    Args:
        pipeline_config: The ``prompt_config`` dict from ``RagPipeline``.

    Returns:
        ``True`` if a shadow call should be executed; ``False`` otherwise.
    """
    ab_cfg: dict[str, Any] = pipeline_config.get("ab_test", {})
    if not ab_cfg.get("enabled", False):
        return False
    split = float(ab_cfg.get("traffic_split", 0.0))
    return random.random() < split  # noqa: S311 — not security-critical


class PromptABTestingService:
    """Runs shadow prompt calls in parallel and records comparison metrics.

    Args:
        session: SQLAlchemy async session used to persist ``ABTestResult`` rows.
            The session must be managed (commit/rollback) by the caller.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_shadow_call(
        self,
        state: QueryState,
        shadow_version: str,
        llm_client: Any,
        model_record: Any,
        control_answer_length: int,
        latency_control_ms: float,
        experiment_id: str,
        control_version: str,
    ) -> ABTestResultDTO | None:
        """Execute the shadow LLM call and return comparison metrics.

        The shadow answer text is consumed only to measure its length; it is
        never stored, logged, or returned to the caller.

        Args:
            state: Current ``QueryState`` (used for context chunks and history).
            shadow_version: Prompt version string for the shadow call, e.g. ``"v2"``.
            llm_client: LLM abstraction client (must have ``chat_completion``).
            model_record: ORM ``ModelsRegistry`` record supplying ``model_id``
                and ``endpoint_url``.
            control_answer_length: Character length of the already-generated
                control answer — passed in so the DTO can be built without
                knowing the control answer text.
            latency_control_ms: Measured latency of the control call in ms.
            experiment_id: Logical experiment identifier from pipeline config.
            control_version: Prompt version used for the control call.

        Returns:
            An ``ABTestResultDTO`` on success, or ``None`` if the shadow call
            fails (errors are caught and logged, never re-raised).
        """
        try:
            prompt_text = _load_prompt_version(shadow_version)
        except FileNotFoundError:
            logger.warning(
                "ab_test.shadow_prompt_not_found",
                shadow_version=shadow_version,
                tenant_id=str(state.tenant_id),
                pipeline_id=str(state.pipeline_id),
            )
            return None

        # Build context identical to what node_generate used for the control call.
        # We intentionally do NOT import node_generate helpers directly to keep
        # the dependency graph clean; the formatting logic is self-contained.
        context_chunks_text = self._build_context(state.graded_chunks)
        history_text = self._format_history(state.conversation_history)

        shadow_prompt = (
            prompt_text.replace("{{QUESTION}}", state.question)
            .replace("{{CONTEXT_CHUNKS}}", context_chunks_text)
            .replace("{{CONVERSATION_HISTORY}}", history_text)
            .replace("{{RESPONSE_LANGUAGE}}", state.response_language or "")
        )

        shadow_start = datetime.now(UTC)
        try:
            shadow_response = await llm_client.chat_completion(
                model=model_record.model_id,
                messages=[{"role": "system", "content": shadow_prompt}],
                base_url=model_record.endpoint_url,
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            shadow_raw = shadow_response.choices[0].message.content or ""
            shadow_answer_length = len(shadow_raw)
        except Exception:
            logger.warning(
                "ab_test.shadow_call_failed",
                shadow_version=shadow_version,
                tenant_id=str(state.tenant_id),
                pipeline_id=str(state.pipeline_id),
                experiment_id=experiment_id,
            )
            return None

        latency_shadow_ms = (datetime.now(UTC) - shadow_start).total_seconds() * 1000.0

        logger.info(
            "ab_test.shadow_completed",
            tenant_id=str(state.tenant_id),
            pipeline_id=str(state.pipeline_id),
            experiment_id=experiment_id,
            control_version=control_version,
            shadow_version=shadow_version,
            control_length=control_answer_length,
            shadow_length=shadow_answer_length,
            latency_control_ms=round(latency_control_ms, 2),
            latency_shadow_ms=round(latency_shadow_ms, 2),
        )

        return ABTestResultDTO(
            experiment_id=experiment_id,
            pipeline_id=state.pipeline_id,
            conversation_id=state.conversation_id,
            tenant_id=state.tenant_id,
            control_prompt_version=control_version,
            shadow_prompt_version=shadow_version,
            control_answer_length=control_answer_length,
            shadow_answer_length=shadow_answer_length,
            latency_control_ms=latency_control_ms,
            latency_shadow_ms=latency_shadow_ms,
            timestamp=datetime.now(UTC),
        )

    async def record_result(self, result: ABTestResultDTO) -> None:
        """Persist an ``ABTestResultDTO`` to the ``ab_test_results`` table.

        The caller is responsible for committing the session.

        Args:
            result: The DTO produced by ``run_shadow_call``.
        """
        row = ABTestResultModel(
            tenant_id=result.tenant_id,
            pipeline_id=result.pipeline_id,
            conversation_id=result.conversation_id,
            experiment_id=result.experiment_id,
            control_version=result.control_prompt_version,
            shadow_version=result.shadow_prompt_version,
            control_length=result.control_answer_length,
            shadow_length=result.shadow_answer_length,
            latency_control_ms=result.latency_control_ms,
            latency_shadow_ms=result.latency_shadow_ms,
        )
        self._session.add(row)

    # ------------------------------------------------------------------
    # Private helpers — kept here to avoid importing from node_generate
    # ------------------------------------------------------------------

    @staticmethod
    def _format_history(history: list[dict[str, str]]) -> str:
        if not history:
            return "(brak historii)"
        return "\n".join(f"{e['role']}: {e['content']}" for e in history)

    @staticmethod
    def _build_context(chunks: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            doc_id = chunk.get("document_id", "unknown")
            page = chunk.get("page_number", "")
            text = chunk.get("highlight_text", "")
            parts.append(f'<chunk index="{i}" document_id="{doc_id}" page="{page}">{text}</chunk>')
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Background task helper used by node_generate
# ---------------------------------------------------------------------------


async def _shadow_task(
    *,
    state: QueryState,
    ab_config: dict[str, Any],
    control_version: str,
    control_answer_length: int,
    latency_control_ms: float,
    llm_client: Any,
    model_record: Any,
    session_factory: Any,
) -> None:
    """Fire-and-forget coroutine: run shadow call and persist result.

    All exceptions are caught so that failures never propagate to the caller.

    Args:
        state: Current query state.
        ab_config: The ``ab_test`` sub-dict from ``prompt_config``.
        control_version: Prompt version used for the main (control) call.
        control_answer_length: Character length of the control answer.
        latency_control_ms: Latency of the control call in ms.
        llm_client: LLM abstraction client.
        model_record: ORM model record for LLM endpoint details.
        session_factory: Async SQLAlchemy session factory (callable returning
            ``AsyncSession``).  Used to create a dedicated DB session for this
            background task.
    """
    shadow_version: str = ab_config.get("shadow_prompt_version", "v2")
    experiment_id: str = ab_config.get("experiment_id", "unknown")

    try:
        async with session_factory() as bg_session:
            service = PromptABTestingService(bg_session)
            dto = await service.run_shadow_call(
                state=state,
                shadow_version=shadow_version,
                llm_client=llm_client,
                model_record=model_record,
                control_answer_length=control_answer_length,
                latency_control_ms=latency_control_ms,
                experiment_id=experiment_id,
                control_version=control_version,
            )
            if dto is not None:
                await service.record_result(dto)
                await bg_session.commit()
    except Exception:
        logger.warning(
            "ab_test.background_task_failed",
            tenant_id=str(state.tenant_id),
            pipeline_id=str(state.pipeline_id),
            experiment_id=experiment_id,
        )


def schedule_shadow_task(
    *,
    state: QueryState,
    ab_config: dict[str, Any],
    control_version: str,
    control_answer_length: int,
    latency_control_ms: float,
    llm_client: Any,
    model_record: Any,
    session_factory: Any,
) -> asyncio.Task[None]:
    """Schedule the shadow call as an asyncio background task.

    Returns the ``asyncio.Task`` so callers can attach done-callbacks or
    reference it in tests.  The task is created with ``asyncio.create_task``
    so it runs concurrently but never blocks the caller.

    Args:
        state: Current query state.
        ab_config: The ``ab_test`` sub-dict from ``prompt_config``.
        control_version: Prompt version used for the control call.
        control_answer_length: Character length of the control answer.
        latency_control_ms: Latency of the control call in ms.
        llm_client: LLM abstraction client.
        model_record: ORM model record for LLM endpoint details.
        session_factory: Async session factory for the background task's DB
            session.

    Returns:
        The background ``asyncio.Task``.
    """
    return asyncio.create_task(
        _shadow_task(
            state=state,
            ab_config=ab_config,
            control_version=control_version,
            control_answer_length=control_answer_length,
            latency_control_ms=latency_control_ms,
            llm_client=llm_client,
            model_record=model_record,
            session_factory=session_factory,
        )
    )
