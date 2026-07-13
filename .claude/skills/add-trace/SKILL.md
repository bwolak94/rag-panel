---
name: add-trace
description: Adds Langfuse instrumentation (tracing) to a graph node or service. Use when a new component needs observability or an existing one is missing traces.
---

# Adding a Langfuse Trace — procedure

1. Read `src/core/langfuse_client.py` — understand existing wrappers and masking configuration.

2. **Security rules (GDPR) — before instrumentation:**
   - Langfuse MUST have active PII masking (verify in the client configuration).
   - Do NOT log: chunk content, prompts with user data, LLM responses, patient names.
   - DO log: `tenant_id`, `session_id`, `trace_id`, node names, timings, numeric metrics.

3. **Span for a graph node** (`src/graphs/*/nodes/<name>.py`):
   ```python
   from src.core.langfuse_client import get_langfuse

   async def node_<name>(state: GraphState) -> dict:
       langfuse = get_langfuse()
       with langfuse.span(
           name="node_<name>",
           trace_id=state.trace_id,
           metadata={"tenant_id": state.tenant_id},  # identifiers only
       ) as span:
           # ... node logic ...
           span.update(output={"chunks_retrieved": len(results)})  # metrics, not content
           return {...}
   ```

4. **Span for a service** (e.g. RetrievalService, DeletionService):
   - Use the decorator `@langfuse_span(name="...", capture_input=False, capture_output=False)` from `src/core/langfuse_client.py` — `capture_input/output=False` blocks content logging.

5. **LLM generation — dedicated `langfuse.generation()` span:**
   ```python
   with langfuse.generation(
       name="generate_response",
       model=model_name,
       usage={"input_tokens": ..., "output_tokens": ...},
       # DO NOT pass prompt/completion containing medical data
   ) as gen:
       ...
   ```

6. Run `docker compose up -d`, make a test query, and verify the trace in Langfuse UI.
7. Verify that no PII reached Langfuse (check in the UI or grep Langfuse logs).
8. Add a test in `tests/unit/test_tracing.py` — mock Langfuse, assert that the span is created and that `capture_input/output` are disabled for PII paths.
