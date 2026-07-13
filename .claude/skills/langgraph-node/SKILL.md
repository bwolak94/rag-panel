---
name: langgraph-node
description: Adds or modifies a LangGraph node (query/ingest graph) following project conventions. Use when changing the RAG pipeline.
---

# LangGraph Node — procedure

1. Determine the graph: `src/graphs/query_graph/` or `src/graphs/ingest_graph/`. A TOPOLOGY change (new edges/nodes) requires `architect` approval and a diagram update in `docs/02-Architektura.md` first.
2. Node = file `nodes/<name>.py`, function `node_<name>(state: GraphState) -> dict` — returns only the changed state fields.
3. LLM calls through the `LLMClient` interface (from `models_registry`), never directly; prompt as a template in `graphs/prompts/<name>_v<N>.md`.
4. Retrieval only through `RetrievalService` (tenant filter applied automatically).
5. Error handling: a node exception must not kill the graph — set error status in state + route to error-handling node; in ingest: retry with backoff, after 3 attempts → `failed`.
6. Tests: unit test the node with a mocked LLM (`tests/unit/graphs/`), cases: success, empty context, LLM error.
7. Any change affecting response quality (prompt, parameters, chunking) → run `pytest tests/eval/`, compare with baseline, report metrics. Regression > 5% = revert or justify.
8. Update node registration in `graph.py` and diagram if applicable.
