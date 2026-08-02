# TASK-031: Audit Log Viewer API

**Status:** TODO
**Priority:** P1 — required for production (US-4.3 from roadmap, Phase 2 deliverable); RODO compliance
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/roadmap.md` §E4 US-4.3, `docs/rodo.md`, `docs/architecture.md` §audit
**Estimated effort:** 3–4 days

---

## Overview

The `audit_log` table already records all security-relevant events (document approve/reject, user actions, access events). However, there is currently no API to query this data — admins cannot review audit trails without direct DB access.

This task implements the **Audit Log Viewer API**: paginated browsing, filtering, and CSV export of audit events. Required for:
- RODO compliance (demonstrating lawful processing, tracking data access)
- Security review (detecting anomalous patterns)
- Debugging (tracing who did what and when)

---

## Usage

**Actor:** Admin (`admin:audit_log` permission), Owner.

**Typical scenario:**
1. RODO audit request arrives: "Who accessed documents in the last 30 days?"
2. Admin calls `GET /admin/audit-log?period=30d&action_prefix=document` → paginated list.
3. Admin exports to CSV: `GET /admin/audit-log/export?format=csv` → CSV download.

---

## Current `audit_log` Schema

```sql
CREATE TABLE audit_log (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    user_id UUID,
    action VARCHAR(100) NOT NULL,         -- e.g. "document.approved", "user.login"
    resource_type VARCHAR(50),             -- e.g. "document", "collection"
    resource_id UUID,
    details JSONB,
    ip_address INET,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

This table exists; this task only adds read/export endpoints.

---

## API Endpoints

All under `/api/v1/admin/audit-log/`. Require `admin:audit_log` permission.

### GET /admin/audit-log

Paginated, filterable audit event list.

**Query params:**

| Param | Type | Description |
|---|---|---|
| `action` | string (optional) | Exact action filter (e.g. `document.approved`) |
| `action_prefix` | string (optional) | Prefix filter (e.g. `document`) |
| `user_id` | UUID (optional) | Filter by actor |
| `resource_type` | string (optional) | `document`, `collection`, `user`, `tenant` |
| `resource_id` | UUID (optional) | Specific resource |
| `from_date` | ISO datetime (optional) | Start of range |
| `to_date` | ISO datetime (optional) | End of range |
| `cursor` | string (optional) | Cursor-based pagination |
| `page_size` | int (default 50, max 200) | |

**Response 200:**
```json
{
  "items": [
    {
      "id": "...",
      "user_id": "...",
      "user_display_name": "Anna Kowalska",
      "action": "document.approved",
      "resource_type": "document",
      "resource_id": "...",
      "details": {"note": "PII verified by admin", "previous_status": "needs_review"},
      "ip_address": "192.168.1.42",
      "created_at": "2026-08-01T10:00:00Z"
    }
  ],
  "total_count": 1847,
  "next_cursor": "...",
  "has_next_page": true
}
```

**Note:** `details` JSONB is returned as-is. It must not contain document content, LLM responses, or PII beyond user/resource identifiers — this is already guaranteed by the existing `AuditService` contract.

### GET /admin/audit-log/export

Export audit log as CSV. Same filter params as above.

**Response:** `Content-Type: text/csv; charset=utf-8` with streaming response.

CSV columns: `id, user_id, user_display_name, action, resource_type, resource_id, details_summary, ip_address, created_at`

`details_summary`: flattened key=value string from JSONB (first 200 chars) — no nesting.

Max export rows: 10,000 (configurable). Larger exports require date range narrowing.

### GET /admin/audit-log/actions

Returns list of distinct action types in the tenant's audit log — for UI filter dropdowns.

```json
{"actions": ["document.approved", "document.rejected", "document.uploaded", "user.login", ...]}
```

---

## Tech Stack

- **Pagination:** cursor-based (same pattern as TASK-012 review queue): cursor = `base64(created_at + ":" + id)`
- **CSV export:** stdlib `csv` + `StreamingResponse` — no temp file, streams directly
- **User display names:** JOIN to `users` table on `user_id`
- **Index:** ensure `audit_log(tenant_id, created_at DESC, id DESC)` index exists (add in migration if missing)

---

## Database Changes

Alembic migration `0017_audit_log_index.py`:
```sql
-- Add missing composite index for efficient filtering
CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_created
    ON audit_log(tenant_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_action
    ON audit_log(tenant_id, action);

CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_resource
    ON audit_log(tenant_id, resource_type, resource_id);
```

No schema changes to `audit_log` table — indices only.

---

## Implementation Steps

1. Alembic migration: add indices to `audit_log`.
2. Add `AuditLogRepository` to `src/db/repositories/` (or extend existing):
   - `list_events(tenant_id, filters, cursor, page_size) -> tuple[list[AuditEvent], bool]`
   - `get_distinct_actions(tenant_id) -> list[str]`
3. `src/domain/audit_log_service.py` — thin service layer (mainly pagination + CSV serialization).
4. Pydantic schemas: `src/api/schemas/audit_log.py`.
5. Router: `src/api/routers/admin_audit_log.py`, register in `main.py`.
6. Add `admin:audit_log` permission (Alembic seed, assign to Admin + Owner roles).
7. CSV streaming: use `StreamingResponse` with `csv.DictWriter` writing to `io.StringIO` in chunks.
8. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- `tenant_id` always from JWT — admins cannot query other tenants' audit logs.
- `ip_address` is included in response — this is expected (audit data by definition).
- `details` JSONB is opaque to this layer — `AuditService` responsibility to not write PII there.
- CSV export is a sensitive operation — log it in `audit_log` itself: action `audit_log.exported`.
- Max 10,000 rows export limit — prevents data exfiltration via bulk export.
- Platform-admin can query all tenants' logs via `?tenant_id=X` override — separate secured endpoint.

---

## Tests

**Unit:**
- Cursor encode/decode round-trips correctly
- CSV streaming produces valid CSV for a fixture dataset
- `get_distinct_actions` returns only this tenant's action types

**Integration:**
- `GET /admin/audit-log` returns 200 for Admin, 403 for Viewer
- Filter by `action=document.approved` returns only matching events
- Filter by date range returns correct subset
- Export action itself appears in audit log after export
- Cross-tenant: admin from tenant A cannot see tenant B's audit events

---

## Definition of Done

- [ ] Indices on `audit_log` (Alembic migration)
- [ ] Paginated list endpoint with all filters
- [ ] CSV export (streaming, max 10k rows)
- [ ] Distinct actions endpoint
- [ ] `admin:audit_log` permission added to Admin + Owner roles
- [ ] CSV export action logged in `audit_log`
- [ ] Tenant isolation: no cross-tenant access
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests
- [ ] `/skill /rodo-audit` checklist: audit trail access documented
