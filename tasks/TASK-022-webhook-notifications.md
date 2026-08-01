# TASK-022: Webhook Notifications for Document and Pipeline Events

**Status:** TODO
**Priority:** P2 — enables external system integration (clinic EHR, Teams/Slack alerts)
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/architecture.md`, `docs/prd.md` §FR-2, `src/core/events/`
**Estimated effort:** 4–5 days

---

## Overview

Tenants need to integrate the RAG platform with external systems: notify a Teams channel when a document fails validation, trigger a downstream EHR workflow when a procedure document is indexed, or alert staff when a document is rejected.

This task implements a **webhook delivery system**: admins register webhook URLs per tenant, select which events trigger delivery, and the platform POSTs a signed JSON payload to the URL on each matching event.

---

## Events Supported (MVP)

| Event type | Trigger |
|---|---|
| `document.uploaded` | File arrives in MinIO |
| `document.ready` | Document fully indexed in Qdrant |
| `document.failed` | Ingest pipeline error (unrecoverable) |
| `document.needs_review` | PII or low quality detected |
| `document.approved` | Admin approves review |
| `document.rejected` | Admin rejects document |
| `ingestion_job.started` | Worker picks up document |
| `ingestion_job.completed` | All ingest steps done |

---

## Database Schema

```sql
CREATE TABLE webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    url TEXT NOT NULL,                          -- HTTPS only
    secret TEXT NOT NULL,                       -- HMAC-SHA256 signing secret (stored hashed)
    events TEXT[] NOT NULL,                     -- list of event types to subscribe
    is_active BOOLEAN DEFAULT TRUE,
    failure_count INTEGER DEFAULT 0,            -- auto-disable after 10 consecutive failures
    last_triggered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    created_by UUID REFERENCES users(id)
);

CREATE TABLE webhook_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id UUID NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(20) NOT NULL,                -- pending | success | failed | retrying
    http_status INTEGER,
    response_body TEXT,                         -- first 500 chars only
    attempt_count INTEGER DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    delivered_at TIMESTAMPTZ
);
```

Alembic migration: `0011_webhooks.py`

---

## API Endpoints

Under `/api/v1/admin/webhooks/`. Require `admin:webhooks` permission.

### CRUD

- `POST /admin/webhooks` — register webhook (validate URL is HTTPS, generate secret)
- `GET /admin/webhooks` — list tenant webhooks
- `GET /admin/webhooks/{id}` — get details (secret masked as `***`)
- `PATCH /admin/webhooks/{id}` — update events/name/active
- `DELETE /admin/webhooks/{id}` — delete
- `POST /admin/webhooks/{id}/test` — send test payload to verify connectivity

### Deliveries

- `GET /admin/webhooks/{id}/deliveries` — delivery log (last 100)
- `POST /admin/webhooks/deliveries/{delivery_id}/retry` — manual retry

---

## Webhook Payload Format

```json
{
  "event": "document.ready",
  "tenant_id": "...",
  "timestamp": "2026-08-01T10:00:00Z",
  "data": {
    "document_id": "...",
    "document_name": "procedura-wypisania.pdf",
    "collection_id": "...",
    "collection_name": "Procedury medyczne",
    "status": "ready"
  }
}
```

**Signature:** `X-RAG-Signature: sha256=<HMAC-SHA256(secret, raw_body)>` — receiver must verify.

---

## Delivery Mechanism

- **Async delivery** via background task (`asyncio.create_task`) — does not block API response.
- **Retry policy:** 3 attempts with exponential backoff (1 min, 5 min, 15 min). After 10 consecutive failures: `webhook.is_active = False`, audit log entry.
- **Timeout:** 10 seconds per HTTP request (`httpx.AsyncClient`).
- **HTTPS only:** URL validation rejects `http://` schemes.
- **Secret storage:** stored as `bcrypt` hash — secret shown once at creation, never again. Signing uses the plaintext secret stored in a separate encrypted column or fetched from a secrets manager.

> MVP simplification: store plaintext secret encrypted with `Fernet` using `SECRET_KEY` from env. Production: Vault/AWS Secrets Manager.

---

## Implementation Steps

1. Alembic migration: `webhooks`, `webhook_deliveries` tables.
2. `src/domain/webhook_service.py`:
   - `register_webhook()`, `list_webhooks()`, `delete_webhook()`
   - `dispatch_event(event_type, tenant_id, data)` — find active matching webhooks, create deliveries, trigger async sends
   - `deliver(delivery_id)` — HTTP POST with signature, update delivery status, handle retry
3. `src/api/routers/admin_webhooks.py` — CRUD endpoints.
4. Hook dispatch into existing event points:
   - `ingest_graph` nodes: call `webhook_service.dispatch_event()` at status transitions
   - `admin_review_service.py`: dispatch `document.approved` / `document.rejected`
5. Add `admin:webhooks` permission (Alembic seed).
6. Background retry worker — use existing Redis or a simple `asyncio` scheduled task.
7. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- HTTPS-only URLs — reject at registration.
- HMAC-SHA256 signature on every delivery — receivers must verify.
- Secret never logged, never returned after creation.
- `tenant_id` from JWT — a tenant can only manage their own webhooks.
- Payload never contains PII from document content — only IDs and metadata.
- Response body truncated to 500 chars in `webhook_deliveries` — avoid storing external data.
- Auto-disable after 10 failures — prevents infinite retries to dead endpoints.

---

## Tests

**Unit:**
- `dispatch_event` finds correct webhooks by event type
- Signature header is correct HMAC-SHA256
- HTTPS validation rejects `http://` URLs
- After 10 failures: webhook marked inactive, audit log entry

**Integration:**
- Register webhook → ingest document → delivery created with `status=success`
- Test endpoint sends payload to mock server
- Cross-tenant: admin A cannot see/modify webhooks of tenant B

---

## Definition of Done

- [ ] `webhooks` and `webhook_deliveries` tables + Alembic migration
- [ ] CRUD endpoints + delivery log endpoint
- [ ] Async delivery with retry and auto-disable
- [ ] HMAC-SHA256 signature on all deliveries
- [ ] Dispatch hooked into ingest graph and admin review
- [ ] `admin:webhooks` permission in DB
- [ ] Tests: unit (signature, retry logic) + integration (happy path + tenant isolation)
- [ ] `docs/03-Specyfikacja-API.md` updated
