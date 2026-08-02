# TASK-026: API Rate Limiting and Per-tenant Quotas

**Status:** TODO
**Priority:** P2 — required before multi-tenant production launch; prevents abuse and resource starvation
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/architecture.md`, `docs/prd.md` §3, `docs/roadmap.md` Phase 3
**Estimated effort:** 3–4 days

---

## Overview

In a multi-tenant environment a single misbehaving tenant (or compromised account) can exhaust shared resources (GPU inference, Qdrant bandwidth, MinIO storage). This task implements:

1. **Rate limiting** — per-user, per-tenant request rate caps enforced in middleware.
2. **Tenant quotas** — configurable limits on documents, storage, monthly queries, and MAU stored in `tenants.settings`.
3. **Quota enforcement** — checked before expensive operations (upload, ingest, chat completion).

---

## Rate Limits (defaults, overridable per tenant)

| Endpoint group | Limit | Window |
|---|---|---|
| `POST /v1/chat/completions` | 20 req/min per user | sliding window |
| `POST /collections/{id}/documents` | 30 req/min per tenant | sliding window |
| `POST /collections/{id}/documents/bulk-import` | 5 req/hour per tenant | fixed window |
| `GET /admin/*` | 60 req/min per user | sliding window |
| `POST /admin/documents/*/approve|reject` | 30 req/min per user | sliding window |
| All others | 120 req/min per user | sliding window |

---

## Tenant Quotas

Stored in `tenants.settings JSONB` (already exists):

```json
{
  "quotas": {
    "max_documents": 500,
    "max_storage_bytes": 5368709120,
    "max_monthly_queries": 10000,
    "max_mau": 50,
    "max_collections": 20
  }
}
```

**Defaults** in `Settings` (pydantic-settings) — overridable per tenant by platform-admin.

---

## Quota Checks

| Operation | Quota checked |
|---|---|
| Document upload | `max_documents`, `max_storage_bytes` |
| Collection create | `max_collections` |
| Chat completion | `max_monthly_queries` (rolling 30d via `mau_service`) |
| User onboarding | `max_mau` |

On quota exceeded: **HTTP 429** with `Retry-After` header and problem detail:
```json
{
  "type": "about:blank",
  "title": "Quota Exceeded",
  "status": 429,
  "detail": "Monthly query quota reached (10000/10000). Resets on 2026-09-01.",
  "quota_type": "monthly_queries",
  "limit": 10000,
  "current": 10000,
  "reset_at": "2026-09-01T00:00:00Z"
}
```

---

## Tech Stack

- **Rate limiting:** `fastapi-limiter` backed by Redis — sliding window algorithm
- **Quota counters:** Redis counters (`INCR` + `EXPIREAT`) for real-time query counting; Postgres aggregates for storage/document counts
- **Middleware:** `src/api/middleware/rate_limit.py` — applies per-endpoint limits using `fastapi-limiter` decorators
- **Quota service:** `src/domain/quota_service.py` — `check_quota()` called before expensive operations

---

## Implementation

### Rate Limiting Middleware

```python
# Applied as FastAPI dependency per router

from fastapi_limiter.depends import RateLimiter

# Chat endpoint
@router.post("/v1/chat/completions",
             dependencies=[Depends(RateLimiter(times=20, seconds=60))])

# Override: per-tenant limit stored in Redis as `rate_limit:{tenant_id}:{endpoint}`
# QuotaService reads tenant override before applying default
```

Custom key function: `f"{ctx.tenant_id}:{ctx.user_id}:{endpoint}"` — per-user within tenant.

### Quota Service

```python
# src/domain/quota_service.py

class QuotaService:
    async def check_document_upload(self, tenant_id: UUID, file_size: int) -> None:
        """Raises QuotaExceededError if max_documents or max_storage_bytes exceeded."""

    async def check_query(self, tenant_id: UUID) -> None:
        """Raises QuotaExceededError if max_monthly_queries exceeded.
        Increments Redis counter on success."""

    async def get_quota_status(self, tenant_id: UUID) -> QuotaStatus:
        """Returns current usage vs limits for all quota types."""
```

### QuotaExceededError

Add to `src/core/exceptions.py`:
```python
class QuotaExceededError(AppError):
    status_code = 429
    quota_type: str
    limit: int
    current: int
    reset_at: datetime | None = None
```

---

## Admin Quota Management

### GET /admin/quota/status

Returns current quota usage for the tenant.

```json
{
  "tenant_id": "...",
  "quotas": {
    "documents": {"limit": 500, "current": 143, "pct": 28.6},
    "storage_bytes": {"limit": 5368709120, "current": 892341234, "pct": 16.6},
    "monthly_queries": {"limit": 10000, "current": 847, "pct": 8.5, "reset_at": "2026-09-01"},
    "mau": {"limit": 50, "current": 12, "pct": 24.0},
    "collections": {"limit": 20, "current": 5, "pct": 25.0}
  }
}
```

### PATCH /platform/tenants/{id}/quotas (platform-admin only)

Update quota limits for a specific tenant.

---

## Database Changes

- `tenants.settings` already exists as JSONB — add quota defaults in seed data.
- Alembic: no schema change needed (uses existing `settings` column).
- Add `quota_events` table for quota breach logging (optional for MVP; use `audit_log` instead).

---

## Implementation Steps

1. Add `fastapi-limiter` to dependencies (`uv add fastapi-limiter`).
2. Initialize `fastapi-limiter` with Redis client in app lifespan.
3. Add `RateLimiter` dependencies to all relevant routers.
4. Implement custom key function using `tenant_id:user_id`.
5. Implement `QuotaService` with Redis counters + Postgres aggregation.
6. Add `QuotaExceededError` and map to 429 in exception handlers.
7. Hook `check_query()` into `ChatService._invoke_graph()`.
8. Hook `check_document_upload()` into document upload handler.
9. Implement `GET /admin/quota/status` endpoint.
10. Implement `PATCH /platform/tenants/{id}/quotas` (platform-admin).
11. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- Rate limit key includes `tenant_id` — tenants cannot affect each other's limits.
- Quota overrides only by platform-admin (verified from JWT role).
- `Retry-After` header always set on 429 — clients must back off.
- Quota counters use Redis atomic `INCR` — no race conditions.

---

## Tests

**Unit:**
- `check_query()` raises `QuotaExceededError` when counter >= limit
- `check_document_upload()` raises when document count or storage at limit
- Redis counter incremented only on successful query (not on graph error)

**Integration:**
- 21st chat request within 60s returns 429 with `Retry-After`
- Quota status endpoint returns correct current usage
- Platform-admin can update quota limits; tenant admin cannot

---

## Definition of Done

- [ ] Rate limiting on all documented endpoint groups
- [ ] `QuotaService` with document, storage, query, MAU checks
- [ ] `QuotaExceededError` → HTTP 429 with problem detail
- [ ] `GET /admin/quota/status` endpoint
- [ ] `PATCH /platform/tenants/{id}/quotas` (platform-admin)
- [ ] Quota defaults in `Settings` (pydantic-settings)
- [ ] Redis-backed rate limit keys include `tenant_id`
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests including rate limit enforcement
