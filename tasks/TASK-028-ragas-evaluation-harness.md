# TASK-028: RAGAS Evaluation Harness — RAG Quality Baseline

**Status:** TODO
**Priority:** P1 — required before production; blocks release per DoD in architecture docs
**Owner:** ml-engineer + rag-engineer
**Reviewer:** python-reviewer
**Related docs:** `docs/roadmap.md` §4 (test plan), `docs/architecture.md`, `docs/prd.md` §US-3.2
**Estimated effort:** 5–7 days

---

## Overview

The roadmap specifies a quality evaluation test suite: 50 question-source pairs + 20 trap questions (hallucination tests). Currently no eval harness exists. This task:

1. Creates the eval dataset (`tests/eval/dataset/`) — Polish medical domain.
2. Implements a pytest-based eval runner using RAGAS metrics.
3. Establishes a **baseline** and gates regressions > 5% in CI.
4. Documents the evaluation protocol in `docs/`.

This is an **improvement** — not a new feature — that makes the existing RAG pipeline measurably reliable.

---

## Eval Dataset Structure

```
tests/eval/
  dataset/
    questions.jsonl         # 50 substantive + 20 trap questions
    ground_truth.jsonl      # expected answers / expected sources
  fixtures/
    sample_documents/       # 5–10 representative PDFs for eval collection
  test_rag_eval.py          # pytest eval runner
  conftest.py               # shared fixtures (collection setup, seeded docs)
  baseline.json             # current metric scores (committed to repo)
  README.md                 # how to run and update baseline
```

### Question Format (`questions.jsonl`)

```json
{"id": "q001", "question": "Jakie są wskazania do zastosowania ibuprofenu?", "type": "substantive", "expected_source_keywords": ["ibuprofen", "NLPZ", "ból"], "collection_hint": "pharmacology"}
{"id": "q051", "question": "Czy morfina leczy cukrzycę?", "type": "trap", "expected_answer_type": "not_found"}
```

---

## RAGAS Metrics

| Metric | Description | Regression threshold |
|---|---|---|
| `faithfulness` | Answer grounded in retrieved context | > 5% drop blocks merge |
| `answer_relevancy` | Answer relevant to the question | > 5% drop blocks merge |
| `context_precision` | Retrieved chunks precision | > 5% drop blocks merge |
| `context_recall` | Retrieved chunks recall | > 5% drop blocks merge |
| `answer_correctness` | vs ground truth (substantive questions) | measured, not a blocker |
| Hallucination rate | Trap questions that produced non-"not found" response | 0 tolerance (any failure blocks) |

---

## Implementation

### Eval Runner (`tests/eval/test_rag_eval.py`)

```python
import pytest
import json
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from tests.eval.conftest import invoke_rag_pipeline, load_dataset

@pytest.mark.eval
class TestRAGEval:
    def test_faithfulness_above_baseline(self, eval_dataset, baseline):
        results = run_eval(eval_dataset, metrics=[faithfulness])
        assert results["faithfulness"] >= baseline["faithfulness"] * 0.95, \
            f"Faithfulness regression: {results['faithfulness']:.3f} < baseline {baseline['faithfulness']:.3f}"

    def test_no_hallucination_on_trap_questions(self, trap_questions):
        for q in trap_questions:
            result = invoke_rag_pipeline(q["question"])
            assert result.answer_type == "not_found", \
                f"Hallucination on trap question '{q['id']}': got '{result.answer[:100]}'"
```

### Eval Fixture (`tests/eval/conftest.py`)

```python
@pytest.fixture(scope="session")
def eval_collection(test_db, test_qdrant):
    """Create a dedicated eval collection with sample documents seeded."""
    # Upload sample_documents/ to test collection
    # Wait for all ingestion_jobs to reach status=ready
    # Yield collection_id
    ...
```

### Baseline Management

```bash
# Update baseline (run after intentional quality improvement)
pytest tests/eval/ -m eval --update-baseline

# Run eval only (CI)
pytest tests/eval/ -m eval --timeout=300
```

`baseline.json` is committed. Updated only intentionally via the `--update-baseline` flag which writes new scores.

---

## CI Integration

`.github/workflows/eval.yml` (separate workflow, not on every PR):
- Triggered on: pushes to `main` + PRs that modify `graphs/`, `retrieval/`, `graphs/prompts/`
- Uses testcontainers for Qdrant + Postgres
- Fails if any RAGAS metric regresses > 5% vs committed `baseline.json`
- Posts metric summary as PR comment

---

## Dataset Creation (one-time)

Initial 50 substantive questions written by:
1. Team brainstorm from `tests/eval/fixtures/sample_documents/` content.
2. LLM-assisted generation: prompt an LLM to generate Q&A pairs from document text.
3. Human review — remove bad pairs, ensure Polish medical terminology.

20 trap questions manually authored — cover common hallucination patterns:
- Questions about topics not in any document
- Questions with false premises ("Does Drug X cure Condition Y?" where it doesn't)
- Questions mixing concepts from different documents misleadingly

---

## Tech Stack

- **RAGAS:** `ragas>=0.2` (latest stable)
- **LLM judge for RAGAS:** same model as query graph (from `models_registry`) — no OpenAI API key required
- **Embeddings for RAGAS:** same embedding model as collection
- **pytest marker:** `@pytest.mark.eval` — excluded from default `pytest` run (`pytest.ini` `addopts = -m "not eval"`)

---

## Implementation Steps

1. Add `ragas` to dev dependencies (`uv add --dev ragas`).
2. Create `tests/eval/` directory structure.
3. Write 50 substantive + 20 trap questions (domain: general medical procedures).
4. Upload 5–10 representative sample PDFs to `tests/eval/fixtures/`.
5. Implement `conftest.py` with session-scoped eval collection fixture.
6. Implement `invoke_rag_pipeline()` helper — calls `invoke_query_graph()` directly.
7. Write `test_rag_eval.py` with all metric tests and hallucination test.
8. Run eval, commit resulting `baseline.json`.
9. Create `.github/workflows/eval.yml`.
10. Add eval documentation to `docs/`.

---

## Definition of Done

- [ ] 50 substantive + 20 trap questions in `questions.jsonl`
- [ ] `test_rag_eval.py` with all RAGAS metrics + hallucination test
- [ ] `baseline.json` committed with initial metric scores
- [ ] `--update-baseline` flag implemented
- [ ] CI workflow triggers on prompt/retrieval/chunking changes
- [ ] Hallucination rate = 0% on trap questions (hard blocker)
- [ ] `pytest -m eval` passes locally with testcontainers
- [ ] `docs/` eval protocol documented
- [ ] `/skill /rag-eval` checklist completed
