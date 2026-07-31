"""Unit tests for PromptABTestingService.

Covers:
- should_run_shadow: disabled config returns False
- should_run_shadow: enabled with traffic_split 1.0 always returns True
- should_run_shadow: enabled with traffic_split 0.0 always returns False
- run_shadow_call: ABTestResultDTO contains no answer content (only lengths)
- run_shadow_call: shadow failure returns None and does not propagate
- run_shadow_call: missing prompt file returns None gracefully
- schedule_shadow_task: fire-and-forget task completes without blocking
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.ab_testing_service import (
    ABTestResultDTO,
    PromptABTestingService,
    schedule_shadow_task,
    should_run_shadow,
)
from src.graphs.query_graph.state import QueryState

# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------

TENANT_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
LLM_MODEL_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()


def _make_state(
    prompt_config: dict[str, Any] | None = None,
    graded_chunks: list[dict[str, Any]] | None = None,
) -> QueryState:
    return QueryState(
        question="Jakie są godziny przyjęć?",
        conversation_id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        allowed_collection_ids=[COLLECTION_ID],
        pipeline_id=PIPELINE_ID,
        llm_model_id=LLM_MODEL_ID,
        collection_ids=[COLLECTION_ID],
        graded_chunks=graded_chunks or [],
        prompt_config=prompt_config or {},
    )


def _make_model_record() -> MagicMock:
    record = MagicMock()
    record.id = LLM_MODEL_ID
    record.model_id = "llama3.2"
    record.endpoint_url = "http://ollama:11434/v1"
    return record


def _make_llm_response(content: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_service() -> PromptABTestingService:
    session = AsyncMock()
    return PromptABTestingService(session=session)


# ---------------------------------------------------------------------------
# should_run_shadow
# ---------------------------------------------------------------------------


def test_should_run_shadow_disabled_returns_false() -> None:
    """When ab_test.enabled is False, should_run_shadow must return False."""
    config = {
        "generate_prompt_version": "v1",
        "ab_test": {
            "enabled": False,
            "shadow_prompt_version": "v2",
            "traffic_split": 1.0,
            "experiment_id": "exp-001",
        },
    }
    assert should_run_shadow(config) is False


def test_should_run_shadow_no_ab_block_returns_false() -> None:
    """When prompt_config has no ab_test key, should_run_shadow returns False."""
    assert should_run_shadow({"generate_prompt_version": "v1"}) is False


def test_should_run_shadow_enabled_traffic_split_one_always_true() -> None:
    """When ab_test.enabled=True and traffic_split=1.0, every call returns True."""
    config = {
        "ab_test": {
            "enabled": True,
            "shadow_prompt_version": "v2",
            "traffic_split": 1.0,
            "experiment_id": "exp-full",
        }
    }
    for _ in range(10):
        assert should_run_shadow(config) is True


def test_should_run_shadow_enabled_traffic_split_zero_always_false() -> None:
    """When ab_test.enabled=True but traffic_split=0.0, every call returns False."""
    config = {
        "ab_test": {
            "enabled": True,
            "shadow_prompt_version": "v2",
            "traffic_split": 0.0,
            "experiment_id": "exp-zero",
        }
    }
    for _ in range(10):
        assert should_run_shadow(config) is False


def test_should_run_shadow_enabled_traffic_split_respects_random() -> None:
    """When traffic_split=0.5, roughly half of calls return True (mocked random)."""
    config = {
        "ab_test": {
            "enabled": True,
            "shadow_prompt_version": "v2",
            "traffic_split": 0.5,
            "experiment_id": "exp-half",
        }
    }
    with patch("src.domain.ab_testing_service.random.random", return_value=0.3):
        assert should_run_shadow(config) is True

    with patch("src.domain.ab_testing_service.random.random", return_value=0.7):
        assert should_run_shadow(config) is False


# ---------------------------------------------------------------------------
# run_shadow_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_shadow_does_not_include_answer_content() -> None:
    """ABTestResultDTO must contain only lengths, not the answer text itself."""
    state = _make_state()
    service = _make_service()
    model_record = _make_model_record()

    shadow_answer = "Odpowiedź cienia w języku polskim — dłuższa niż kontrolna."
    llm_client = AsyncMock()
    llm_client.chat_completion = AsyncMock(
        return_value=_make_llm_response(json.dumps({"answer": shadow_answer, "citations": []}))
    )

    # Provide a real v2 prompt file path via patch so the test is file-system agnostic.
    fake_prompt = (
        "Odpowiedz: {{QUESTION}}\nKontekst: {{CONTEXT_CHUNKS}}\nHistoria: {{CONVERSATION_HISTORY}}"
    )
    with patch("src.domain.ab_testing_service._load_prompt_version", return_value=fake_prompt):
        dto = await service.run_shadow_call(
            state=state,
            shadow_version="v2",
            llm_client=llm_client,
            model_record=model_record,
            control_answer_length=20,
            latency_control_ms=120.0,
            experiment_id="exp-001",
            control_version="v1",
        )

    assert dto is not None
    assert isinstance(dto, ABTestResultDTO)

    # Verify lengths are recorded, not text content.
    # The raw LLM content includes JSON wrapping, so we check it is an int > 0.
    assert isinstance(dto.shadow_answer_length, int)
    assert dto.shadow_answer_length > 0
    assert dto.control_answer_length == 20

    # Crucially: the DTO has no attribute carrying answer text.
    assert not hasattr(dto, "shadow_answer")
    assert not hasattr(dto, "control_answer")
    assert not hasattr(dto, "answer")
    assert not hasattr(dto, "question")


@pytest.mark.asyncio
async def test_run_shadow_result_has_correct_metadata() -> None:
    """ABTestResultDTO carries correct experiment_id, versions, and tenant identifiers."""
    state = _make_state()
    service = _make_service()
    model_record = _make_model_record()

    llm_client = AsyncMock()
    llm_client.chat_completion = AsyncMock(
        return_value=_make_llm_response(json.dumps({"answer": "Odpowiedź.", "citations": []}))
    )

    fake_prompt = "Prompt: {{QUESTION}} {{CONTEXT_CHUNKS}} {{CONVERSATION_HISTORY}}"
    with patch("src.domain.ab_testing_service._load_prompt_version", return_value=fake_prompt):
        dto = await service.run_shadow_call(
            state=state,
            shadow_version="v2",
            llm_client=llm_client,
            model_record=model_record,
            control_answer_length=50,
            latency_control_ms=200.0,
            experiment_id="exp-metadata",
            control_version="v1",
        )

    assert dto is not None
    assert dto.experiment_id == "exp-metadata"
    assert dto.control_prompt_version == "v1"
    assert dto.shadow_prompt_version == "v2"
    assert dto.tenant_id == TENANT_ID
    assert dto.pipeline_id == PIPELINE_ID
    assert dto.conversation_id == CONVERSATION_ID
    assert dto.latency_control_ms == pytest.approx(200.0)
    assert dto.latency_shadow_ms >= 0.0


@pytest.mark.asyncio
async def test_shadow_failure_does_not_propagate() -> None:
    """When the shadow LLM call raises an exception, run_shadow_call returns None."""
    state = _make_state()
    service = _make_service()
    model_record = _make_model_record()

    llm_client = AsyncMock()
    llm_client.chat_completion = AsyncMock(side_effect=RuntimeError("LLM connection refused"))

    fake_prompt = "Prompt: {{QUESTION}} {{CONTEXT_CHUNKS}} {{CONVERSATION_HISTORY}}"
    with patch("src.domain.ab_testing_service._load_prompt_version", return_value=fake_prompt):
        dto = await service.run_shadow_call(
            state=state,
            shadow_version="v2",
            llm_client=llm_client,
            model_record=model_record,
            control_answer_length=30,
            latency_control_ms=150.0,
            experiment_id="exp-fail",
            control_version="v1",
        )

    assert dto is None


@pytest.mark.asyncio
async def test_shadow_missing_prompt_file_returns_none() -> None:
    """When the shadow prompt file does not exist, run_shadow_call returns None gracefully."""
    state = _make_state()
    service = _make_service()
    model_record = _make_model_record()
    llm_client = AsyncMock()

    # Force FileNotFoundError from the prompt loader
    with patch(
        "src.domain.ab_testing_service._load_prompt_version",
        side_effect=FileNotFoundError("generate_v99.md not found"),
    ):
        dto = await service.run_shadow_call(
            state=state,
            shadow_version="v99",
            llm_client=llm_client,
            model_record=model_record,
            control_answer_length=10,
            latency_control_ms=100.0,
            experiment_id="exp-missing",
            control_version="v1",
        )

    assert dto is None
    llm_client.chat_completion.assert_not_called()


# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_result_adds_row_to_session() -> None:
    """record_result calls session.add with an ABTestResult ORM instance."""
    from src.db.models.ab_test_result import ABTestResult as ABTestResultModel

    session = AsyncMock()
    service = PromptABTestingService(session=session)

    from datetime import UTC, datetime

    dto = ABTestResultDTO(
        experiment_id="exp-record",
        pipeline_id=PIPELINE_ID,
        conversation_id=CONVERSATION_ID,
        tenant_id=TENANT_ID,
        control_prompt_version="v1",
        shadow_prompt_version="v2",
        control_answer_length=42,
        shadow_answer_length=55,
        latency_control_ms=180.0,
        latency_shadow_ms=210.0,
        timestamp=datetime.now(UTC),
    )

    await service.record_result(dto)

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert isinstance(added, ABTestResultModel)
    assert added.experiment_id == "exp-record"
    assert added.control_length == 42
    assert added.shadow_length == 55
    assert added.tenant_id == TENANT_ID


# ---------------------------------------------------------------------------
# schedule_shadow_task (fire-and-forget)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_shadow_task_completes_without_blocking() -> None:
    """schedule_shadow_task returns a Task that completes successfully."""
    state = _make_state()
    model_record = _make_model_record()

    llm_client = AsyncMock()
    llm_client.chat_completion = AsyncMock(
        return_value=_make_llm_response(json.dumps({"answer": "Shadow OK.", "citations": []}))
    )

    fake_session = AsyncMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()

    session_factory = MagicMock(return_value=fake_session)

    fake_prompt = "Prompt: {{QUESTION}} {{CONTEXT_CHUNKS}} {{CONVERSATION_HISTORY}}"

    ab_config = {
        "enabled": True,
        "shadow_prompt_version": "v2",
        "traffic_split": 1.0,
        "experiment_id": "exp-task",
    }

    with patch("src.domain.ab_testing_service._load_prompt_version", return_value=fake_prompt):
        task = schedule_shadow_task(
            state=state,
            ab_config=ab_config,
            control_version="v1",
            control_answer_length=30,
            latency_control_ms=120.0,
            llm_client=llm_client,
            model_record=model_record,
            session_factory=session_factory,
        )

        # Yield to event loop so the task can complete.
        await asyncio.sleep(0)
        await task

    assert task.done()
    assert task.exception() is None


@pytest.mark.asyncio
async def test_shadow_task_exception_does_not_propagate() -> None:
    """If the entire shadow task raises, the exception is caught and not re-raised."""
    state = _make_state()
    model_record = _make_model_record()
    llm_client = AsyncMock()

    # session_factory itself raises to simulate a DB connection error
    def _broken_factory() -> Any:
        raise RuntimeError("DB pool exhausted")

    ab_config = {
        "enabled": True,
        "shadow_prompt_version": "v2",
        "traffic_split": 1.0,
        "experiment_id": "exp-broken",
    }

    with patch("src.domain.ab_testing_service._load_prompt_version", return_value="Prompt"):
        task = schedule_shadow_task(
            state=state,
            ab_config=ab_config,
            control_version="v1",
            control_answer_length=0,
            latency_control_ms=0.0,
            llm_client=llm_client,
            model_record=model_record,
            session_factory=_broken_factory,
        )
        await asyncio.sleep(0)
        await task

    # Task completed (exception was caught internally by _shadow_task)
    assert task.done()
