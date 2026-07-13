---
name: new-endpoint
description: Adds a new REST endpoint following project conventions (RBAC, audit log, tests, documentation). Use when adding or modifying an API endpoint.
---

# New API Endpoint — procedure

1. Read `docs/03-Specyfikacja-API.md` — is the endpoint already described? If not, first write the contract (method, path, permission, schemas, error codes) and present it to the user for approval.
2. Pydantic schemas in `src/api/schemas/<resource>.py` (Request/Response, examples in `json_schema_extra`).
3. Router in `src/api/routers/<resource>.py`:
   - dependency `require("<permission>")` + `get_current_ctx()`,
   - router delegates to a service in `src/domain/` — zero business logic in the router,
   - resource-level authorization: service verifies `resource.tenant_id == ctx.tenant_id`.
4. Any mutating action → write to `audit_log` in the same transaction.
5. Tests in `tests/integration/api/test_<resource>.py`:
   - happy path,
   - 401 (missing token), 403 (every role without the required permission — parameterize),
   - 404 for a resource belonging to a DIFFERENT tenant (not 403 — don't reveal existence),
   - 422 validation.
6. Run `ruff check --fix . && mypy src/ && pytest -x -q`.
7. Update `docs/03-Specyfikacja-API.md` if the contract changed during implementation.
