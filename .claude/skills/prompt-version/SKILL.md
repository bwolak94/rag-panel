---
name: prompt-version
description: Versions a new or modified prompt following RAG conventions (graphs/prompts/, changelog). Use whenever prompt content is changed.
---

# Prompt Versioning — procedure

1. Locate the existing template in `src/graphs/prompts/` (format: `<name>_v<N>.md`).
2. **Never edit an existing version** — create a new file `<name>_v<N+1>.md`.
3. Prompt file structure:

```markdown
# <name> v<N+1>

**Node:** <node_name>
**Date:** YYYY-MM-DD
**Change:** <one-sentence description of the difference from the previous version>

---

<template content with placeholders {context}, {question}, etc.>
```

4. Prompt content security rules:
   - Wrap chunk context with an XML delimiter: `<context>...</context>` and add an instruction to ignore commands inside it.
   - Never insert user data directly — always use placeholders.
   - Every answer must include citations or an explicit "not found in documents" message.
5. Update the reference in the graph node (`node_<name>.py`) to point to the new version.
6. Append an entry to `src/graphs/prompts/CHANGELOG.md`:
   ```
   ## v<N+1> — YYYY-MM-DD
   - <what changed and why>
   ```
7. **Required: run evaluation:** `/rag-eval` — compare metrics against baseline. Regression > 5% = revert the change or justify in the PR.
8. Keep the old prompt file in the repository (version history — do not delete).
