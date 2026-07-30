#!/usr/bin/env python3
"""Baseline updater for the RAG evaluation suite.

Run this script after an intentional, verified improvement to the pipeline
(e.g. a better model, improved prompt, or higher-quality chunking strategy)
to record the new reference metrics in baseline.json.

IMPORTANT: Only run this when you have confirmed the improvement is genuine.
Running it to silence a regression test without fixing the root cause defeats
the purpose of the evaluation harness.

Usage:
    python tests/eval/update_baseline.py

The script will:
1. Execute the eval suite locally (no CI overhead).
2. Compute the four metrics from the run.
3. Prompt the user to confirm before overwriting baseline.json.
4. Write the new baseline with the current timestamp and git commit hash.

Requirements:
    - A local Python environment with dev dependencies installed (uv sync).
    - No running inference server is required — the suite uses mocked LLM calls.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

EVAL_DIR = Path(__file__).parent
BASELINE_PATH = EVAL_DIR / "baseline.json"
DATASET_PATH = EVAL_DIR / "dataset.json"

METRIC_KEYS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")


# ---------------------------------------------------------------------------
# Inline scoring (mirrors test_rag_quality.py — keep in sync)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ]{3,}", text)}


def _overlap_f1(ref: str, cand: str) -> float:
    ref_t = _tokenize(ref)
    cand_t = _tokenize(cand)
    if not ref_t or not cand_t:
        return 0.0
    inter = ref_t & cand_t
    p = len(inter) / len(cand_t)
    r = len(inter) / len(ref_t)
    return 2 * p * r / (p + r) if (p + r) else 0.0


_REFUSAL_RE = re.compile(
    r"nie mogłem znaleźć|nie znalazłem|brak informacji|nie ma informacji"
    r"|nie zawiera.*odpowied|niedostępna w dokumentach|nie dotyczy"
    r"|poza zakresem|nie posiadam informacji",
    re.IGNORECASE,
)


def _is_refusal(answer: str) -> bool:
    return bool(_REFUSAL_RE.search(answer))


def _faithfulness(answer: str, contexts: list[str]) -> float:
    if _is_refusal(answer):
        return 1.0
    tokens = _tokenize(answer)
    if not tokens:
        return 0.0
    ctx_tokens = _tokenize(" ".join(contexts))
    return len(tokens & ctx_tokens) / len(tokens)


def _relevancy(answer: str, question: str) -> float:
    if _is_refusal(answer):
        return 0.5
    return _overlap_f1(question, answer)


def _precision(contexts: list[str], question: str) -> float:
    if not contexts:
        return 0.0
    useful = sum(1 for c in contexts if _overlap_f1(c, question) > 0.05)
    return useful / len(contexts)


def _recall(contexts: list[str], ground_truth: str) -> float:
    sentences = [s.strip() for s in re.split(r"[.!?]", ground_truth) if len(s.strip()) > 10]
    if not sentences:
        return 0.0
    ctx = " ".join(contexts)
    covered = sum(1 for s in sentences if _overlap_f1(s, ctx) > 0.2)
    return covered / len(sentences)


def _mock_answer(row: dict) -> str:
    """Produce the same mock answer as conftest.py / medical_answers fixture.

    Mirrors the pattern in test_rag_quality.py::medical_answers — keep in sync.
    """
    if row["trap"]:
        return "Na podstawie dostępnych dokumentów nie mogłem znaleźć odpowiedzi na to pytanie."
    excerpt = row["contexts"][0][:300] if row["contexts"] else ""
    question_stem = row["question"].rstrip("?")
    return f"W odpowiedzi na pytanie: {question_stem}. {excerpt} [1]"


def _compute_metrics(dataset: list[dict]) -> dict[str, float]:
    """Run the mock pipeline over the dataset and compute all four metrics."""
    faith, relev, prec, rec = [], [], [], []
    for row in dataset:
        if row["trap"]:
            continue  # trap questions are excluded from metric computation
        answer = _mock_answer(row)
        faith.append(_faithfulness(answer, row["contexts"]))
        relev.append(_relevancy(answer, row["question"]))
        prec.append(_precision(row["contexts"], row["question"]))
        rec.append(_recall(row["contexts"], row["ground_truth"]))

    def _mean(vals: list[float]) -> float:
        return round(sum(vals) / len(vals), 6) if vals else 0.0

    return {
        "faithfulness": _mean(faith),
        "answer_relevancy": _mean(relev),
        "context_precision": _mean(prec),
        "context_recall": _mean(rec),
    }


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_commit_hash() -> str:
    """Return the current HEAD short commit hash, or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Compute metrics, display comparison, prompt for confirmation, write baseline."""
    with open(DATASET_PATH, encoding="utf-8") as fh:
        dataset = json.load(fh)

    print("Computing metrics from mock pipeline (no LLM required)...")
    new_metrics = _compute_metrics(dataset)

    # Load existing baseline for comparison
    existing: dict = {}
    if BASELINE_PATH.exists():
        with open(BASELINE_PATH, encoding="utf-8") as fh:
            existing = json.load(fh)

    print()
    print(f"{'Metric':<22} {'New':>9} {'Current':>9} {'Delta':>9}")
    print("-" * 52)
    for key in METRIC_KEYS:
        new_val = new_metrics[key]
        cur_val = existing.get(key, 0.0)
        delta = new_val - cur_val
        sign = "+" if delta >= 0 else ""
        print(f"{key:<22} {new_val:>9.4f} {cur_val:>9.4f} {sign}{delta:>8.4f}")
    print()

    # Safety check: refuse to write a worse baseline
    regressions = [k for k in METRIC_KEYS if new_metrics[k] < existing.get(k, 0.0) - 1e-6]
    if regressions:
        print(
            f"WARNING: new metrics are LOWER than the current baseline for: {regressions}.\n"
            "You should only update the baseline after a genuine improvement.\n"
            "If this regression is intentional, edit baseline.json manually.",
        )
        answer = input("Write the new (lower) baseline anyway? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. baseline.json was not changed.")
            sys.exit(0)
    else:
        answer = input("Write new baseline? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. baseline.json was not changed.")
            sys.exit(0)

    commit = _git_commit_hash()
    timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    payload: dict = {
        **new_metrics,
        "timestamp": timestamp,
        "commit": commit,
        "notes": existing.get("notes", ""),
    }

    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"baseline.json updated (commit={commit}, timestamp={timestamp}).")


if __name__ == "__main__":
    main()
