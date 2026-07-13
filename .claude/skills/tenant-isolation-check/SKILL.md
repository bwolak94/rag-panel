---
name: tenant-isolation-check
description: Audits tenant isolation in the codebase. Use after every change to auth/RBAC/retrieval/repositories and before any release.
---

# Tenant Isolation Audit — procedure

1. **Qdrant outside RetrievalService:**
   - `grep -rn "QdrantClient\|qdrant_client" src/ --include="*.py"` — allowed only in `src/retrieval/` and `src/core/clients.py`. Any other occurrence = BLOCKER.
2. **SQL queries without tenant filter:**
   - Review repositories in `src/db/`; every method that lists or fetches business resources must filter by `tenant_id` from context (not from the request parameter!).
   - `grep -rn "text(\|execute(" src/ --include="*.py"` — raw SQL requires justification.
3. **Tenant from wrong source:**
   - `grep -rn "tenant_id" src/api/` — `tenant_id` must not come from body/query/path; only from JWT (ctx).
4. **MinIO:** paths built from `ctx.tenant_id`, not from user input; presigned URL generated after verifying document access rights.
5. **Isolation tests:** run `pytest -m tenant_isolation`. Verify that tests exist for: (a) retrieval for tenant A does not return chunks from B, (b) GET resource of another tenant → 404, (c) resource list contains only own tenant's resources, (d) ingest event from tenant A does not write to B.
6. Report: table [area | status OK/FAIL | file:line | recommendation]. Every FAIL = merge blocker.
