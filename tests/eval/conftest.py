"""Eval-specific pytest fixtures.

All fixtures here are designed to be completely offline — no real LLM calls
are made. The mock LLM scores responses deterministically so the eval suite
runs in well under 60 seconds on any CI runner.

Architecture note:
    The eval harness uses a custom LLM-as-judge implementation rather than
    importing RAGAS directly, so that:
    - Tests are fast (no HTTP calls to Ollama/vLLM).
    - CI does not require a GPU or a running inference server.
    - Scores are reproducible across runs.

    The ``MockJudgeLLM`` returns pre-programmed scores for known
    (question, answer, context) triplets. Unknown triplets fall back to
    conservative defaults so regressions are visible without false passes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

EVAL_DIR = Path(__file__).parent
DATASET_PATH = EVAL_DIR / "dataset.json"
BASELINE_PATH = EVAL_DIR / "baseline.json"


# ---------------------------------------------------------------------------
# Dataset / baseline loaders
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def eval_dataset() -> list[dict[str, Any]]:
    """Load the full evaluation dataset from dataset.json.

    Returns a list of dicts, each with keys:
        question    – the user's question (str)
        ground_truth – expected answer (str)
        contexts    – list of document chunks that contain the answer (list[str])
        trap        – True for out-of-scope questions that must NOT be answered (bool)
    """
    with open(DATASET_PATH, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def medical_questions(eval_dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Subset: 15 medical questions that have answers in the provided context."""
    return [row for row in eval_dataset if not row["trap"]]


@pytest.fixture(scope="session")
def trap_questions(eval_dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Subset: 5 out-of-scope questions — model must decline, not hallucinate."""
    return [row for row in eval_dataset if row["trap"]]


@pytest.fixture(scope="session")
def baseline() -> dict[str, float]:
    """Load baseline metrics from baseline.json.

    Keys: faithfulness, answer_relevancy, context_precision, context_recall.
    Values are floats in [0.0, 1.0].
    """
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    return {
        k: v
        for k, v in data.items()
        if k in {"faithfulness", "answer_relevancy", "context_precision", "context_recall"}
    }


# ---------------------------------------------------------------------------
# Mock LLM that returns deterministic judgement scores
# ---------------------------------------------------------------------------


def _build_mock_generate_answer(dataset: list[dict[str, Any]]) -> AsyncMock:
    """Return an AsyncMock that simulates a RAG pipeline generating answers.

    For medical (non-trap) questions the mock returns a concise answer derived
    from the context, with a citation.  For trap questions the mock returns
    the canonical "not found in documents" refusal phrase that the generate
    prompt mandates.

    This allows the judge metrics to score real behaviour patterns without
    an actual LLM being available in CI.
    """
    question_to_row: dict[str, dict[str, Any]] = {row["question"]: row for row in dataset}

    async def _generate(question: str, contexts: list[str]) -> str:
        row = question_to_row.get(question)
        if row is None:
            return "Na podstawie dostępnych dokumentów nie mogłem znaleźć odpowiedzi."
        if row["trap"]:
            return "Na podstawie dostępnych dokumentów nie mogłem znaleźć odpowiedzi na to pytanie."
        # For answerable questions the mock produces a short answer that:
        # - Opens with a restatement of the question (raises answer_relevancy)
        # - Is grounded in the first context chunk (keeps faithfulness high)
        # - Is shorter than the full ground_truth (exercises recall < 1.0)
        context_excerpt = row["contexts"][0][:300] if row["contexts"] else ""
        # Strip the trailing question mark and build a declarative opener
        question_stem = question.rstrip("?")
        return f"W odpowiedzi na pytanie: {question_stem}. {context_excerpt} [1]"

    mock = AsyncMock(side_effect=_generate)
    return mock


@pytest.fixture(scope="session")
def mock_generate_answer(eval_dataset: list[dict[str, Any]]) -> AsyncMock:
    """Session-scoped mock that generates deterministic answers for the dataset."""
    return _build_mock_generate_answer(eval_dataset)


# ---------------------------------------------------------------------------
# Environment guard: ensure no real inference server is contacted
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _block_real_llm_calls(monkeypatch_session: pytest.MonkeyPatch) -> None:
    """Override LLM_BASE_URL and EMBEDDING_BASE_URL to a non-routable address.

    This is a safety net — if any eval test accidentally constructs a real
    HTTP client it will fail fast instead of hanging or leaking data.
    """
    monkeypatch_session.setenv("LLM_BASE_URL", "http://127.0.0.1:0/v1")
    monkeypatch_session.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:0/v1")


@pytest.fixture(scope="session")
def monkeypatch_session(request: pytest.FixtureRequest) -> pytest.MonkeyPatch:
    """Session-scoped monkeypatch (pytest built-in is function-scoped only)."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()
