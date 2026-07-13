# Security Rules (GDPR — medical data!)

- **Tenant isolation is sacred:** every business table has `tenant_id`; every SQL query filtered by tenant from JWT context; Qdrant only through `RetrievalService` (automatic tenant+collections filter). New code with a direct Qdrant client outside `retrieval/` = rejected.
- Resource-level authorization, not just endpoint-level: verify `resource.tenant_id == ctx.tenant_id` before returning or modifying anything.
- JWT: signature verification (Keycloak JWKS), `exp`, `aud`; roles/tenant exclusively from the token, never from body/query.
- No secrets in the repo; configuration via pydantic-settings; examples in `.env.example` with empty values.
- Logs: no document content, prompts, responses, or PII; use identifiers instead of data (user_id, doc_id). Langfuse with masking enabled.
- Upload: MIME and size validation (100 MB), filename sanitized, file is never executed; presigned URL TTL ≤ 5 min.
- Chunk content in prompts must be treated as untrusted data (prompt injection): delimiters, no execution of instructions from documents, output guardrails.
- Data deletion (GDPR Art. 17): always cascading Postgres → Qdrant → MinIO through `DeletionService`; new data type = update this service.
- Changes touching auth/RBAC/retrieval require: tenant isolation test + review by the `python-reviewer` agent with the security profile.
