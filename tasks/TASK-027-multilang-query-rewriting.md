# TASK-027: Multi-language Query Rewriting and Cross-language Retrieval

**Status:** TODO
**Priority:** P2 — important for Polish medical context; staff may query in Polish, docs in Polish+English
**Owner:** rag-engineer + ml-engineer
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md` §query graph, `docs/prd.md` §FR-4, `docs/reference-repos.md`
**Estimated effort:** 4–5 days

---

## Overview

The pilot clinic has documents in both Polish and English (international clinical guidelines). Staff ask questions in Polish. Without cross-language retrieval, a Polish query misses English-language chunks.

This task adds **language detection** and **cross-language query rewriting** to the query graph:
1. Detect query language (`node_classify_intent` extended or new `node_detect_language`).
2. If collection's primary language differs from query language, translate/rewrite the query.
3. Run retrieval with both original and translated query (parallel), merge results.
4. Generate response in query's detected language.

---

## Architecture

### Extended Query Graph

```
classify_intent → detect_language → rewrite_query (language-aware) → [translate_query if needed]
→ retrieve (original + translated, merged) → grade_documents → generate (respond in user language) → guardrails
```

### Language Config

Per-collection: `primary_language: str` (e.g., `"pol"`, `"eng"`) stored in `collections.chunk_config` JSON.

Per-pipeline: `response_language: "auto" | "pol" | "eng"` — `auto` responds in the user's query language.

---

## New State Fields (`query_graph/state.py`)

```python
detected_language: str | None = None        # ISO 639-3 code: "pol", "eng"
translated_query: str | None = None         # query translated to collection language
cross_language_retrieval: bool = False       # True when translation was performed
response_language: str = "pol"              # language for final generation
```

---

## New Node: `node_detect_language`

**File:** `src/graphs/query_graph/nodes/node_detect_language.py`

**Input:** `state.rewritten_query`
**Output:** `state.detected_language`

**Implementation:** Use `langdetect` library (fast, no LLM call needed). Fallback: `"pol"` if confidence < 0.8.

```python
from langdetect import detect, LangDetectException

def node_detect_language(state: QueryState) -> QueryState:
    try:
        lang = detect(state.rewritten_query)
        state.detected_language = lang  # "pl" → normalize to "pol"
    except LangDetectException:
        state.detected_language = "pol"  # default
    return state
```

---

## Extended Node: `node_translate_query` (new)

**File:** `src/graphs/query_graph/nodes/node_translate_query.py`

**Trigger:** routing edge fires when `detected_language != collection.primary_language`

**Implementation:** LLM call via abstracted client from `models_registry`:

Prompt (`graphs/prompts/query_translation_v1.txt`):
```
Translate the following query from {source_lang} to {target_lang}.
Preserve medical terminology. Return only the translated text, nothing else.

Query: {query}
```

Sets `state.translated_query`, `state.cross_language_retrieval = True`.

---

## Extended Retrieval

`node_retrieve.py` extended: when `cross_language_retrieval=True`:
1. Run `RetrievalService.retrieve(state.rewritten_query, ...)` → dense results A
2. Run `RetrievalService.retrieve(state.translated_query, ...)` → dense results B
3. Merge A+B (deduplicate by `chunk_id`), re-rank by score, take top_k.

Both searches run with `asyncio.gather` for parallelism.

---

## Extended Generation

`node_generate.py` extended: system prompt includes `respond_in_{language}` instruction when `response_language != "eng"`.

Prompt extension (`graphs/prompts/generate_v2.txt`):
- Add section: `"Respond in {response_language}. Do not switch languages mid-response."`

This is a prompt version bump — `/skill /prompt-version` required.

---

## Config Changes

- `collections` table: `primary_language VARCHAR(10) DEFAULT 'pol'`
- Alembic migration: `0015_collection_language.py`
- `pipelines.config` (JSONB): add `response_language: "auto" | "pol" | "eng"` (default `"auto"`)

---

## Implementation Steps

1. Alembic migration: `primary_language` column on `collections`.
2. Implement `node_detect_language.py` using `langdetect`.
3. Implement `node_translate_query.py` with versioned prompt.
4. Update query graph routing: insert language detection after `rewrite_query`; conditional edge to translation.
5. Extend `node_retrieve.py` for parallel retrieval + merge.
6. Extend `node_generate.py` for language-aware response instruction.
7. Version generate prompt (v1 → v2).
8. Langfuse spans: `language_detection`, `query_translation`.
9. Update `docs/02-Architektura.md` query graph diagram.

---

## Language Code Normalization

`langdetect` returns ISO 639-1 (`pl`, `en`). Normalize to ISO 639-3 for Tesseract/embedding model compatibility:

```python
LANG_MAP = {"pl": "pol", "en": "eng", "de": "deu", "fr": "fra"}
```

---

## Tests

**Unit:**
- `node_detect_language` identifies Polish and English queries correctly
- `node_translate_query` calls LLM with correct source/target lang; output stored in `translated_query`
- `node_retrieve` runs parallel searches when `cross_language_retrieval=True`; deduplicates by `chunk_id`
- Routing skips translation when query language == collection language

**Integration:**
- Polish query against English-primary collection → translated query used → English chunks retrieved
- Response is in Polish (matches query language when `response_language="auto"`)
- `langdetect` confidence < 0.8 → default language used, no crash

---

## Definition of Done

- [ ] `primary_language` on `collections` + Alembic migration
- [ ] `node_detect_language` implemented (no LLM, no blocking I/O)
- [ ] `node_translate_query` implemented with versioned prompt
- [ ] Parallel dual-language retrieval in `node_retrieve`
- [ ] Generate prompt updated (v2) with language instruction
- [ ] Langfuse spans for new nodes
- [ ] Routing updated in `query_graph`
- [ ] `docs/02-Architektura.md` diagram updated
- [ ] `/skill /prompt-version` changelog entry (generate v2)
- [ ] Unit + integration tests
