# TASK-021: Per-tenant Usage Analytics API

**Status:** DONE
**Priority:** P2 — required for billing, quotas, and admin dashboards
**Owner:** backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md`, `docs/prd.md` §4 (Administration), `docs/data-model.md`
**Estimated effort:** 3–4 days

---

## Overview

Administrators and platform owners need visibility into how their tenant uses the platform: number of active users, queries per day, documents ingested, tokens consumed, and error rates. The `mau_service.py` already tracks Monthly Active Users; this task extends it into a full analytics API.

Data is aggregated from existing tables (`conversations`, `messages`, `documents`, `ingestion_jobs`, `audit_log`) — no new event tables needed for MVP. Aggregated views are cached in Redis (TTL 300s) to avoid expensive COUNT queries on every request.

---

## Usage

**Actors:** `Admin` (own tenant), `platform-admin` (all tenants)

**Typical scenario:**
1. Admin opens the analytics dashboard.
2. `GET /admin/analytics/summary` returns: active users (30d), total queries (7d), documents indexed, avg response latency, error rate.
3. Admin drills into `GET /admin/analytics/queries?period=7d&granularity=day` for a daily query volume chart.

---

## API Endpoints

All under `/api/v1/admin/analytics/`. Require `admin:analytics` permission.

### GET /admin/analytics/summary

Returns tenant-level summary for the last 30 days.

**Response:**
```json
{
  "tenant_id": "...",
  "period_days": 30,
  "active_users": 12,
  "total_queries": 847,
  "total_documents": 143,
  "documents_ready": 138,
  "documents_failed": 5,
  "documents_needs_review": 3,
  "avg_query_latency_ms": 1240,
  "error_rate_pct": 0.8,
  "top_collections": [
    {"collection_id": "...", "name": "Procedury medyczne", "query_count": 412}
  ],
  "generated_at": "2026-08-01T10:00:00Z",
  "cached": true
}
```

### GET /admin/analytics/queries

Daily/weekly query volume time series.

**Query params:** `period=7d|30d|90d`, `granularity=hour|day|week`

**Response:**
```json
{
  "series": [
    {"date": "2026-07-25", "query_count": 112, "unique_users": 8, "error_count": 1},
    {"date": "2026-07-26", "query_count": 98, "unique_users": 7, "error_count": 0}
  ],
  "total_queries": 847,
  "period": "7d",
  "granularity": "day"
}
```

### GET /admin/analytics/documents

Document ingest statistics.

**Response:**
```json
{
  "by_status": {"ready": 138, "failed": 5, "needs_review": 3, "processing": 2},
  "by_collection": [
    {"collection_id": "...", "name": "...", "document_count": 67, "total_size_bytes": 45678900}
  ],
  "ingest_success_rate_pct": 96.5,
  "avg_ingest_duration_ms": 8400
}
```

### GET /admin/analytics/users

MAU and active user breakdown.

**Response:**
```json
{
  "mau_30d": 12,
  "dau_7d": [
    {"date": "2026-07-25", "active_users": 6}
  ],
  "top_users_by_queries": [
    {"user_id": "...", "display_name": "Anna K.", "query_count": 87}
  ]
}
```

---

## Tech Stack

- **Aggregation:** Raw SQL via SQLAlchemy Core (`text()`) — not ORM (performance for COUNT/GROUP BY)
- **Cache:** Redis with key `analytics:{tenant_id}:{endpoint}:{params_hash}`, TTL 300s
- **Permissions:** New permission `admin:analytics` — add to `Admin` and `Owner` roles via Alembic seed
- **Service:** `src/domain/analytics_service.py`
- **Router:** `src/api/routers/admin_analytics.py`

---

## Implementation Steps

1. Create `src/domain/analytics_service.py` with methods:
   - `get_summary(tenant_id, period_days) -> SummaryResponse`
   - `get_query_series(tenant_id, period, granularity) -> QuerySeriesResponse`
   - `get_document_stats(tenant_id) -> DocumentStatsResponse`
   - `get_user_stats(tenant_id, period_days) -> UserStatsResponse`
2. Each method: try Redis cache → miss → run SQL aggregation → set cache → return.
3. Create Pydantic schemas in `src/api/schemas/analytics.py`.
4. Create router `src/api/routers/admin_analytics.py`, register in `main.py`.
5. Add `admin:analytics` permission: Alembic migration assigning to Admin + Owner roles.
6. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- `tenant_id` always from JWT — analytics data is scoped per tenant, never cross-tenant.
- `top_users_by_queries` exposes `user_id` and `display_name` — ensure `display_name` is not PII-rich (use first name + last initial; configurable).
- Cache key includes `tenant_id` — no cross-tenant cache pollution.
- Platform-admin role can query `?tenant_id=X` override — platform-admin only endpoint, not exposed to tenant admins.

---

## Tests

**Unit:**
- `get_summary()` returns cached value on second call (mock Redis hit)
- SQL aggregations return correct counts from fixture data
- Cache TTL is set to 300s (not more)

**Integration:**
- `GET /admin/analytics/summary` returns 200 for Admin, 403 for Viewer
- Tenant A analytics cannot include Tenant B data
- Cache is invalidated after TTL (or manually via `DELETE /admin/analytics/cache` — Admin only)

---

## Definition of Done

- [ ] All 4 endpoints implemented with Pydantic schemas
- [ ] Redis cache with TTL 300s
- [ ] `admin:analytics` permission added via migration
- [ ] Tenant isolation: all queries filtered by `tenant_id` from JWT
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests
- [ ] No PII in logs (user_id used as identifier, not name)
