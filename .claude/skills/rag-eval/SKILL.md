---
name: rag-eval
description: Runs RAG quality evaluation and compares results against the baseline. Use after changes to prompts, retrieval, chunking, or models.
---

# RAG Evaluation — procedure

1. Ensure the test stack is running (`docker compose up -d` + seed test corpus `tests/eval/fixtures/`).
2. Run `pytest tests/eval/ -q` — dataset: 50 question→expected-source pairs + 20 trap questions (outside the corpus).
3. Collect metrics:
   - **retrieval:** recall@k, precision@k (is the expected document in the top-k?),
   - **generation:** faithfulness (answer grounded in context), answer relevance, citation accuracy (cited source contains the claim),
   - **traps:** % of "not found in documents" answers (target: 100%; any other answer = hallucination — print it).
4. Compare against baseline in `tests/eval/baseline.json`; table: metric | baseline | current | Δ.
5. Regression > 5% on any metric → mark as BLOCKER, identify the suspect change (git log since last baseline).
6. Improvement with conscious acceptance → update `baseline.json` in the same PR with justification.
7. Append results to `docs/benchmarks/eval-log.md` (date, commit, model config, metrics).
