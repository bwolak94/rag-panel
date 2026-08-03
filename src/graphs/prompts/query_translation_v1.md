# Query Translation Prompt
# Version: 1
# Used by: node_translate_query
# Returns: plain translated text (no JSON wrapping)
#
# Changelog:
# v1 (2026-08-03): Initial version for TASK-027 multi-language query rewriting.

Translate the following query from {source_lang} to {target_lang}.

Rules:
- Preserve all medical terminology accurately — do not simplify or paraphrase.
- Return ONLY the translated query text. Do not include explanations, notes, or alternatives.
- Do not add any prefix like "Translation:" or similar.
- If the query is already in {target_lang}, return it unchanged.

Query: {query}
