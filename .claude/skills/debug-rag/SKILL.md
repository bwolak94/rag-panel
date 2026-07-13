---
name: debug-rag
description: Systematic diagnosis of RAG response quality problems (hallucinations, no results, bad citations, slow retrieval). Use when responses are poor quality or the pipeline behaves unexpectedly.
---

# RAG Debugging — diagnostic procedure

## Step 1 — Collect problem data

- Exact user question, expected answer, received answer.
- `trace_id` from Langfuse (if available) — open the trace, inspect each node.
- Tenant and collection searched.

## Step 2 — Retrieval diagnosis

```bash
# How many chunks were actually retrieved?
# Check the retrieve node in Langfuse or add a temporary debug log
```

Diagnostic questions:
- Is `recall@k` for this question >= threshold? Check top-k result scores.
- Was the query rewritten by `rewrite_query`? Did the rewrite help or hurt?
- Did the `tenant_id` + `allowed_collections` filter narrow results too aggressively?
- Did `grade_documents` discard good chunks? (score threshold too strict)

Check config: `top_k`, `score_threshold` in `src/core/config.py`.

## Step 3 — Generation diagnosis

- Do the chunks in context actually contain the answer? (manual verification)
- If yes but LLM still hallucinates → problem with the generation prompt or model.
- If no → retrieval problem (Step 2).
- Check the `guardrails_output` node — did it incorrectly block a valid answer?

## Step 4 — Chunking diagnosis

- Find the source document in MinIO.
- Check how it is split into chunks (Postgres: `chunks` table, `content` column).
- Did a chunk boundary cut through key information? → Change `chunk_size` / `overlap` for this document type.

## Step 5 — Remediation

| Problem                              | Action                                              |
|--------------------------------------|-----------------------------------------------------|
| Bad retrieval (chunks don't match)   | Adjust `top_k`, `score_threshold`, or embedding model |
| Rewrite degrades query               | Fix rewrite prompt (`/prompt-version`)              |
| Grader discards good chunks          | Adjust grading threshold or grader prompt           |
| Hallucination despite good context   | Update generation prompt (`/prompt-version`)        |
| Bad chunk boundaries                 | Change chunking strategy for the collection         |

## Step 6 — Verification

After every change run `/rag-eval` — compare metrics against baseline.
Document the problem found and the solution in `docs/benchmarks/eval-log.md`.
