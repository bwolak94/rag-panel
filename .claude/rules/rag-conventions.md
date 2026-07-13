# RAG / LangGraph Conventions

- Graphs: `graphs/query_graph/` and `graphs/ingest_graph/`; state as a Pydantic model in `state.py`; Postgres checkpointer.
- Query graph nodes (order): classify_intent → rewrite_query → retrieve → grade_documents → generate → guardrails_output → persist. Any topology change = diagram update in `docs/02-Architektura.md`.
- Prompts: exclusively in `graphs/prompts/` as versioned templates (name + version); inline prompts in code are forbidden; every prompt change = entry in the prompt changelog.
- Substantive responses always include citations (`message_sources`); no context → explicit "not found in documents", never guessing.
- Retrieval: top_k=8 default, configurable per pipeline; score threshold cuts noise; parameters in config, not hardcoded.
- Chunking: strategy per document type from `collections.chunk_config`; default recursive 512 tokens / overlap 64.
- Embeddings: model per collection (`models_registry`); Qdrant collection name = `emb_{embedding_model_slug}`; model change = full re-indexing — never mix vectors from different models in a single Qdrant collection.
- Evaluation: every prompt/retrieval/chunking change triggers `tests/eval/` (50 questions + 20 trap questions); faithfulness/relevance regression > 5% blocks merge.
- LLM through abstraction (OpenAI-compatible client from `models_registry`); binding code to a specific provider is forbidden.
- Ingest: every stage reports status to `ingestion_jobs`; operations are idempotent (document hash, upsert by deterministic point_id).
