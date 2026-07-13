---
name: adr
description: Creates or updates an Architecture Decision Record in docs/02-Architektura.md. Use before any new feature, technology change, or when a documented decision is missing.
---

# ADR — procedure

1. Read `docs/02-Architektura.md` — check whether a similar decision already exists (avoid duplicates).
2. Identify the ADR number (next after the last one in the document).
3. Fill in the ADR template:

```markdown
## ADR-<N>: <title>

**Status:** Proposed / Accepted / Superseded by ADR-<X>
**Date:** YYYY-MM-DD

### Context
<Problem to solve; technical / business / GDPR constraints>

### Decision
<What we are doing and why>

### Rejected alternatives
- <Option A> — <reason for rejection>
- <Option B> — <reason for rejection>

### Consequences
- ✅ <positives>
- ⚠️ <trade-offs / risks>
```

4. Append the ADR to `docs/02-Architektura.md` in the ADR section (maintain chronological order).
5. If this ADR supersedes a previous one, update the old entry's status to `Superseded by ADR-<N>`.
6. Present the draft to the user for approval before anything is implemented — this is a hard project rule: no code without a documented decision.
7. After approval, list implementation tasks with agent assignments (backend-dev / rag-engineer / microservices).
