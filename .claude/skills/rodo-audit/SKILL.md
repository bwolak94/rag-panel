---
name: rodo-audit
description: GDPR compliance audit for a new feature or module (medical data). Use before every release and after adding a new data type or PII flow.
---

# GDPR Audit — procedure

1. Read `docs/05-Bezpieczenstwo-RODO.md` — identify the data categories processed by the modified module.

2. **Personal data inventory:**
   - What PII / medical data flows through the new or changed code path?
   - Where is it stored (Postgres, Qdrant, MinIO, logs, cache)?
   - How long is it retained (retention policy)?

3. **Log audit** — `grep -rn "log\.\(info\|debug\|warning\|error\)" src/ --include="*.py"` in changed files:
   - No document content, prompts, LLM responses, or PII in logs.
   - Only identifiers: `user_id`, `doc_id`, `tenant_id`, `job_id`.

4. **Langfuse audit:**
   - Check masking configuration in `src/core/langfuse_client.py`.
   - Prompt/response must not reach Langfuse without active PII masking.

5. **Right to erasure (Art. 17):**
   - Is the new data type handled by `DeletionService`?
   - Verify cascade: Postgres → Qdrant → MinIO for new tables/collections/buckets.
   - `grep -rn "DeletionService" src/` — is the new resource covered?

6. **Presigned URL and file upload:**
   - Presigned URL TTL ≤ 5 minutes.
   - MIME validation + size ≤ 100 MB.
   - Filename sanitized (`pathlib.Path(name).name`).

7. **Encryption:**
   - Data at rest: MinIO and Postgres volumes encrypted (infra config, not code).
   - Data in transit: TLS required (check `docker-compose.yml`).

8. **Report:** table [area | status OK/WARN/FAIL | file:line | recommendation].
   - FAIL = release blocker.
   - WARN = technical debt task for the next sprint.
   - Append results to `docs/05-Bezpieczenstwo-RODO.md` in the audit history section.
