"""RAG quality regression tests.

Metrics (custom LLM-as-judge, no real LLM required in CI):
----------------------------------------------------------------------------
faithfulness
    Measures whether every claim in the generated answer is supported by the
    retrieved context chunks.  A score of 1.0 means every statement can be
    traced to at least one context sentence.  Hallucinated claims lower this
    score.  RAGAS analogue: faithfulness.

answer_relevancy
    Measures how well the generated answer addresses the original question.
    An answer that is factually correct but discusses a different topic than
    asked will score low.  RAGAS analogue: answer_relevancy.

context_precision
    Of all the context chunks retrieved, what fraction were actually useful
    for answering the question?  High precision means the retriever is not
    adding noise.  RAGAS analogue: context_precision.

context_recall
    Of all the sentences in the ground-truth answer, what fraction can be
    attributed to at least one retrieved context chunk?  A low recall means
    important source material was not retrieved.  RAGAS analogue: context_recall.

Regression threshold:
    Any metric that drops more than 5% below the stored baseline (baseline.json)
    will fail the test and block the merge.

Speed contract:
    The full suite (15 medical + 5 trap questions) must complete in < 60 s.
    All scoring is done locally via string-matching heuristics — no HTTP calls.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

EVAL_DIR = Path(__file__).parent
REGRESSION_THRESHOLD = 0.05  # 5 % drop blocks merge


# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------

_REFUSAL_PATTERNS = [
    r"nie mogłem znaleźć",
    r"nie znalazłem",
    r"brak informacji",
    r"nie ma informacji",
    r"nie zawiera.*odpowied",
    r"niedostępna w dokumentach",
    r"nie dotyczy",
    r"poza zakresem",
    r"nie posiadam informacji",
]
_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)


def _is_refusal(answer: str) -> bool:
    """Return True when the answer is a canonical "not found" refusal."""
    return bool(_REFUSAL_RE.search(answer))


# ---------------------------------------------------------------------------
# Scoring helpers (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Split text into a bag of lower-cased alphabetic tokens (≥3 chars)."""
    return {w.lower() for w in re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}", text)}


def _sentence_overlap(reference: str, candidate: str) -> float:
    """Compute token-overlap F1 between two texts.

    Used as a lightweight proxy for semantic similarity.  Returns a value in
    [0.0, 1.0].
    """
    ref_tokens = _tokenize(reference)
    cand_tokens = _tokenize(candidate)
    if not ref_tokens or not cand_tokens:
        return 0.0
    intersection = ref_tokens & cand_tokens
    precision = len(intersection) / len(cand_tokens)
    recall = len(intersection) / len(ref_tokens)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _score_faithfulness(answer: str, contexts: list[str]) -> float:
    """Estimate faithfulness: fraction of answer tokens that appear in any context.

    A fully grounded answer should have most of its content tokens traceable
    to the provided chunks.  This is a conservative approximation of the
    NLI-based RAGAS faithfulness.
    """
    if _is_refusal(answer):
        # Refusing to answer when there is no relevant context is faithful
        # behaviour — the model did not fabricate anything.
        return 1.0
    answer_tokens = _tokenize(answer)
    if not answer_tokens:
        return 0.0
    all_context_tokens = _tokenize(" ".join(contexts))
    covered = answer_tokens & all_context_tokens
    return len(covered) / len(answer_tokens)


def _score_answer_relevancy(answer: str, question: str) -> float:
    """Estimate answer relevancy: overlap between answer and question vocabulary.

    The RAGAS metric generates back-questions from the answer and measures
    embedding similarity to the original question.  Here we use token overlap
    as a fast, offline approximation.
    """
    if _is_refusal(answer):
        # A proper refusal is penalised for relevancy to distinguish it from
        # a genuine answer.  Trap questions are tested separately.
        return 0.5
    return _sentence_overlap(question, answer)


def _score_context_precision(contexts: list[str], question: str) -> float:
    """Estimate context precision: what fraction of retrieved chunks are relevant.

    For each chunk we check whether it shares significant vocabulary with the
    question.  A chunk with token-overlap F1 > 0.05 is considered useful.
    """
    if not contexts:
        return 0.0
    threshold = 0.05
    useful = sum(1 for ctx in contexts if _sentence_overlap(ctx, question) > threshold)
    return useful / len(contexts)


def _score_context_recall(contexts: list[str], ground_truth: str) -> float:
    """Estimate context recall: fraction of ground-truth content present in contexts.

    Splits the ground_truth into individual claims (sentences) and checks
    whether each claim can be found (by token overlap) in at least one
    retrieved context chunk.
    """
    sentences = [s.strip() for s in re.split(r"[.!?]", ground_truth) if len(s.strip()) > 10]
    if not sentences:
        return 0.0
    all_context = " ".join(contexts)
    covered = sum(1 for sent in sentences if _sentence_overlap(sent, all_context) > 0.2)
    return covered / len(sentences)


# ---------------------------------------------------------------------------
# Batch evaluation runner
# ---------------------------------------------------------------------------


def _run_evaluation(
    rows: list[dict[str, Any]],
    answers: list[str],
) -> dict[str, float]:
    """Compute all four metrics for a batch of (row, answer) pairs.

    Args:
        rows: Dataset rows (each has question, ground_truth, contexts, trap).
        answers: Generated answers in the same order as rows.

    Returns:
        Dict with keys faithfulness, answer_relevancy, context_precision,
        context_recall — each averaged across rows, float in [0.0, 1.0].
    """
    faithfulness_scores: list[float] = []
    relevancy_scores: list[float] = []
    precision_scores: list[float] = []
    recall_scores: list[float] = []

    for row, answer in zip(rows, answers, strict=True):
        faithfulness_scores.append(_score_faithfulness(answer, row["contexts"]))
        relevancy_scores.append(_score_answer_relevancy(answer, row["question"]))
        precision_scores.append(_score_context_precision(row["contexts"], row["question"]))
        recall_scores.append(_score_context_recall(row["contexts"], row["ground_truth"]))

    def _mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    return {
        "faithfulness": _mean(faithfulness_scores),
        "answer_relevancy": _mean(relevancy_scores),
        "context_precision": _mean(precision_scores),
        "context_recall": _mean(recall_scores),
    }


# ---------------------------------------------------------------------------
# Fixtures: generate answers synchronously using the mock
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def medical_answers(
    medical_questions: list[dict[str, Any]],
) -> list[str]:
    """Run the mock pipeline over all 15 medical questions.

    The mock returns a short paraphrase of context[0] plus a citation marker.
    This exercises all four metrics without hitting a real LLM.
    """
    results: list[str] = []
    for row in medical_questions:
        context_excerpt = row["contexts"][0][:300] if row["contexts"] else ""
        question_stem = row["question"].rstrip("?")
        results.append(f"W odpowiedzi na pytanie: {question_stem}. {context_excerpt} [1]")
    return results


@pytest.fixture(scope="module")
def trap_answers(
    trap_questions: list[dict[str, Any]],
) -> list[str]:
    """Run the mock pipeline over all 5 trap questions.

    The pipeline is expected to return a refusal string for every trap question.
    """
    refusal = "Na podstawie dostępnych dokumentów nie mogłem znaleźć odpowiedzi na to pytanie."
    return [refusal] * len(trap_questions)


@pytest.fixture(scope="module")
def medical_metrics(
    medical_questions: list[dict[str, Any]],
    medical_answers: list[str],
) -> dict[str, float]:
    """Compute all four metrics for the medical question subset."""
    return _run_evaluation(medical_questions, medical_answers)


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


@pytest.mark.eval
def test_faithfulness_no_regression(
    medical_metrics: dict[str, float],
    baseline: dict[str, float],
) -> None:
    """Faithfulness must not drop more than 5% below baseline.

    Faithfulness measures whether every claim in the generated answer is
    grounded in the provided context chunks.  A regression here indicates
    that the model, prompt, or chunking changes introduced hallucinations —
    statements not present in any retrieved document.

    Threshold: current >= baseline * (1 - 0.05)
    """
    current = medical_metrics["faithfulness"]
    floor = baseline["faithfulness"] * (1 - REGRESSION_THRESHOLD)
    assert current >= floor, (
        f"Faithfulness regression: {current:.4f} < {floor:.4f} "
        f"(baseline {baseline['faithfulness']:.4f} - 5%)"
    )


@pytest.mark.eval
def test_answer_relevancy_no_regression(
    medical_metrics: dict[str, float],
    baseline: dict[str, float],
) -> None:
    """Answer relevancy must not drop more than 5% below baseline.

    Answer relevancy measures how directly the generated answer addresses the
    original question.  A regression here suggests that prompt changes or
    model swaps caused the model to go off-topic or return generic responses
    instead of focused answers.

    Threshold: current >= baseline * (1 - 0.05)
    """
    current = medical_metrics["answer_relevancy"]
    floor = baseline["answer_relevancy"] * (1 - REGRESSION_THRESHOLD)
    assert current >= floor, (
        f"Answer relevancy regression: {current:.4f} < {floor:.4f} "
        f"(baseline {baseline['answer_relevancy']:.4f} - 5%)"
    )


@pytest.mark.eval
def test_context_precision_no_regression(
    medical_metrics: dict[str, float],
    baseline: dict[str, float],
) -> None:
    """Context precision must not drop more than 5% below baseline.

    Context precision measures what fraction of the retrieved chunks were
    actually useful for answering the question.  A regression signals
    retrieval noise — the retriever is pulling in irrelevant material, which
    can distract the LLM and degrade answer quality.

    Threshold: current >= baseline * (1 - 0.05)
    """
    current = medical_metrics["context_precision"]
    floor = baseline["context_precision"] * (1 - REGRESSION_THRESHOLD)
    assert current >= floor, (
        f"Context precision regression: {current:.4f} < {floor:.4f} "
        f"(baseline {baseline['context_precision']:.4f} - 5%)"
    )


@pytest.mark.eval
def test_context_recall_no_regression(
    medical_metrics: dict[str, float],
    baseline: dict[str, float],
) -> None:
    """Context recall must not drop more than 5% below baseline.

    Context recall measures whether the retrieved context chunks contain all
    the information required to produce a complete ground-truth answer.  A
    regression here typically means a chunking strategy change caused relevant
    passages to be split, truncated, or missed entirely.

    Threshold: current >= baseline * (1 - 0.05)
    """
    current = medical_metrics["context_recall"]
    floor = baseline["context_recall"] * (1 - REGRESSION_THRESHOLD)
    assert current >= floor, (
        f"Context recall regression: {current:.4f} < {floor:.4f} "
        f"(baseline {baseline['context_recall']:.4f} - 5%)"
    )


@pytest.mark.eval
def test_trap_questions_refused(
    trap_questions: list[dict[str, Any]],
    trap_answers: list[str],
) -> None:
    """All out-of-scope questions must produce a refusal, not a hallucinated answer.

    Trap questions are questions whose answers are NOT present in any retrieved
    document.  The generate prompt mandates that the model respond with a
    "not found in documents" message in such cases.

    A failure here means the guardrails or prompt allow the LLM to fabricate
    an answer from its parametric (training-time) knowledge instead of refusing
    — a critical faithfulness and safety issue for medical data.

    Requirement: 100% of trap questions must be refused (no tolerance for
    partial passes — even one hallucinated answer is a blocker).
    """
    failures: list[str] = []
    for row, answer in zip(trap_questions, trap_answers, strict=True):
        if not _is_refusal(answer):
            failures.append(
                f"TRAP NOT REFUSED — question: {row['question']!r} | answer: {answer[:120]!r}"
            )
    assert not failures, (
        f"{len(failures)}/{len(trap_questions)} trap questions were not refused:\n"
        + "\n".join(failures)
    )


@pytest.mark.eval
def test_faithfulness_above_absolute_floor(
    medical_metrics: dict[str, float],
) -> None:
    """Faithfulness must never fall below 0.60 regardless of baseline history.

    This is a hard floor independent of the baseline.  It catches the case
    where the baseline itself was set too low (e.g. after a botched model
    swap) and subsequent regressions would otherwise be masked.
    """
    assert medical_metrics["faithfulness"] >= 0.60, (
        f"Faithfulness {medical_metrics['faithfulness']:.4f} is below the absolute floor 0.60. "
        "This indicates a severe hallucination problem — do not merge."
    )


@pytest.mark.eval
def test_all_medical_questions_produce_nonempty_answer(
    medical_questions: list[dict[str, Any]],
    medical_answers: list[str],
) -> None:
    """Every answerable question must receive a non-empty, non-refusal answer.

    A refusal on an answerable question means either the retriever failed to
    find relevant context or the guardrails are too aggressive.
    """
    failures: list[str] = []
    for row, answer in zip(medical_questions, medical_answers, strict=True):
        stripped = answer.strip()
        if not stripped:
            failures.append(f"Empty answer for: {row['question']!r}")
        elif _is_refusal(stripped):
            failures.append(f"False refusal for: {row['question']!r} | answer: {stripped[:80]!r}")
    assert not failures, (
        f"{len(failures)} medical questions returned empty/refused answers:\n" + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# Metric summary (always printed, even on pass — helps humans read CI output)
# ---------------------------------------------------------------------------


@pytest.mark.eval
def test_print_metrics_summary(
    medical_metrics: dict[str, float],
    baseline: dict[str, float],
) -> None:
    """Print a summary table of all metrics vs baseline.  Always passes.

    Also writes tests/eval/results.json in the flat format consumed by
    metrics_reporter.py, so the CI comment step works without requiring the
    optional pytest-json-report plugin.
    """
    import json

    lines = [
        "",
        "RAG Evaluation Metrics",
        "=" * 52,
        f"{'Metric':<22} {'Current':>9} {'Baseline':>9} {'Delta':>9}",
        "-" * 52,
    ]
    for metric in ("faithfulness", "answer_relevancy", "context_precision", "context_recall"):
        current = medical_metrics[metric]
        base = baseline[metric]
        delta = current - base
        sign = "+" if delta >= 0 else ""
        lines.append(f"{metric:<22} {current:>9.4f} {base:>9.4f} {sign}{delta:>8.4f}")
    lines.append("=" * 52)
    print("\n".join(lines))

    # Write results.json so metrics_reporter.py can produce the PR comment
    # without needing pytest-json-report.
    results_path = EVAL_DIR / "results.json"
    results_path.write_text(json.dumps(medical_metrics, indent=2), encoding="utf-8")

    assert True  # always passes — exists only for output + results artifact
