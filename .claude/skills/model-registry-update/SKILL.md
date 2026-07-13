---
name: model-registry-update
description: Registers a new LLM or embedding model in the project's models_registry. Use when adding a new provider (Ollama, vLLM), a new model, or changing a model version.
---

# models_registry Update — procedure

1. Read `src/core/config.py` (or `src/core/models_registry.py`) — understand the model registry structure.
2. Read `docs/02-Architektura.md` — models section (OpenAI-compatible interface, abstract client).

3. **Registration types:**

   **LLM model (generation):**
   ```python
   "model_id": {
       "provider": "ollama" | "vllm" | "openai_compatible",
       "base_url": "${OLLAMA_URL}",  # from env — never hardcoded
       "model_name": "llama3.2:8b",
       "context_window": 128000,
       "supports_system_message": True,
       "max_output_tokens": 4096,
   }
   ```

   **Embedding model:**
   ```python
   "embedding_id": {
       "provider": "ollama",
       "model_name": "nomic-embed-text",
       "vector_size": 768,
       "max_tokens": 512,  # embedding model context limit
   }
   ```

4. **Config via env** — URLs and secrets ONLY through pydantic-settings:
   - Add the new key to `.env.example` with an empty value and a comment.
   - Never hardcode a URL or API key in the source.

5. **Test the new model:**
   ```bash
   python -m src.core.model_health_check --model-id <model_id>
   ```
   Checks: endpoint availability, response correctness, latency.

6. **Changing the embedding model for a collection = re-indexing required** — run `/reindex-collection` after registration.

7. **Changing the generation model** → run `/rag-eval` — compare faithfulness and answer relevance against baseline.

8. Update `docs/02-Architektura.md` — "Models" section with a new entry (provider, model, purpose, date added).
