# TASK-032: Tenant Self-Service Onboarding Wizard API

**Status:** TODO
**Priority:** P2 — enables scaling to new clients without manual platform-admin intervention
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/prd.md` §FR-3 (onboarding), `docs/roadmap.md` Phase 3, `docs/architecture.md`
**Estimated effort:** 4–5 days

---

## Overview

Currently, onboarding a new tenant requires manual platform-admin steps: create tenant in DB, configure Keycloak realm, create collections, seed roles. This is a bottleneck for scaling to multiple clients.

This task implements a **guided onboarding wizard API** — a stateful multi-step flow that a platform-admin (or future self-service portal) calls to set up a new tenant end-to-end:

1. **Step 1:** Create tenant (name, slug, settings/quotas)
2. **Step 2:** Configure collections (names, chunking strategy, languages, visibility)
3. **Step 3:** Assign initial admin user (Keycloak user ID + role)
4. **Step 4:** Configure LLM pipeline (model selection, guardrails)
5. **Step 5:** Confirm and activate

Each step is idempotent — calling it again with the same input is safe (upsert semantics). The wizard state is tracked in a `onboarding_sessions` table so it can be resumed if interrupted.

---

## Database Schema

```sql
CREATE TABLE onboarding_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'in_progress',  -- in_progress | completed | abandoned
    current_step INTEGER NOT NULL DEFAULT 1,
    steps_completed JSONB NOT NULL DEFAULT '{}',  -- {"1": true, "2": true, ...}
    draft_config JSONB NOT NULL DEFAULT '{}',     -- accumulated config across steps
    created_by UUID NOT NULL,                      -- platform-admin user_id
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL               -- now() + 7 days
);
```

Alembic migration: `0018_onboarding_sessions.py`

---

## API Endpoints

All under `/api/v1/platform/onboarding/`. Require `platform:admin` role (JWT claim).

### POST /platform/onboarding/sessions

Start a new onboarding session.

**Body:**
```json
{
  "tenant_name": "Przychodnia Medyczna Zdrowie",
  "tenant_slug": "przychodnia-zdrowie",
  "contact_email": "admin@przychodnia.pl"
}
```

**Response 201:**
```json
{
  "session_id": "...",
  "tenant_id": "...",
  "current_step": 1,
  "steps_total": 5,
  "next_step_url": "/api/v1/platform/onboarding/sessions/{id}/steps/1"
}
```

### GET /platform/onboarding/sessions/{session_id}

Get session status and completed steps summary.

### POST /platform/onboarding/sessions/{session_id}/steps/1 — Tenant Configuration

Configure tenant settings and quotas.

**Body:**
```json
{
  "display_name": "Przychodnia Medyczna Zdrowie",
  "settings": {
    "quotas": {"max_documents": 300, "max_mau": 30, "max_monthly_queries": 5000},
    "guardrail_footer": "Odpowiedzi nie stanowią porady medycznej.",
    "retention_days": 730
  }
}
```

### POST /platform/onboarding/sessions/{session_id}/steps/2 — Collections

Create 1–10 collections for the tenant.

**Body:**
```json
{
  "collections": [
    {
      "name": "Procedury medyczne",
      "description": "Procedury i standardy kliniczne",
      "primary_language": "pol",
      "chunk_strategy": "recursive",
      "chunk_size": 512,
      "chunk_overlap": 64,
      "visibility": "internal"
    }
  ]
}
```

Idempotent: if collection with same `name` already exists for tenant, updates it.

### POST /platform/onboarding/sessions/{session_id}/steps/3 — Initial Admin User

Assign the first admin user to the tenant.

**Body:**
```json
{
  "keycloak_user_id": "kc-user-uuid",
  "display_name": "Jan Kowalski",
  "email": "jan.kowalski@przychodnia.pl",
  "role": "admin"
}
```

Creates a `users` + `user_tenant` record. Sends welcome email (if email service configured; otherwise skipped).

### POST /platform/onboarding/sessions/{session_id}/steps/4 — Pipeline Configuration

Configure default RAG pipeline.

**Body:**
```json
{
  "pipeline_name": "Domyslny pipeline RAG",
  "llm_model": "mistral-7b",
  "embedding_model": "mxbai-embed-large",
  "top_k": 8,
  "score_threshold": 0.7,
  "cache_responses": false,
  "guardrails_enabled": true
}
```

Creates pipeline record linked to tenant's collections.

### POST /platform/onboarding/sessions/{session_id}/steps/5 — Confirm and Activate

**Body:** `{}` (no params — confirmation action)

- Validates all required steps (1–4) are completed.
- Sets `tenants.is_active = True`.
- Sets `onboarding_sessions.status = 'completed'`.
- Writes `audit_log` entry: `tenant.activated`.
- Returns summary of created resources.

**Response:**
```json
{
  "tenant_id": "...",
  "tenant_slug": "przychodnia-zdrowie",
  "collections_created": 3,
  "pipeline_created": true,
  "admin_user_assigned": true,
  "activated_at": "2026-08-01T12:00:00Z",
  "login_url": "https://auth.your-platform.com/realms/your-realm"
}
```

### DELETE /platform/onboarding/sessions/{session_id}

Abandon session — sets status `abandoned`. Does **not** delete created resources (admin must clean up manually). Safe to call if onboarding fails halfway.

---

## Implementation Steps

1. Alembic migration: `onboarding_sessions` table.
2. Add `is_active BOOLEAN DEFAULT FALSE` to `tenants` if not present (or use existing field).
3. `src/domain/onboarding_service.py`:
   - `create_session(platform_admin_id, initial_data) -> OnboardingSession`
   - `execute_step(session_id, step_number, step_data, platform_admin_id) -> StepResult`
   - `confirm_and_activate(session_id) -> ActivationResult`
4. Each step method: validate input → upsert resources → update `steps_completed` JSONB → update `current_step`.
5. `src/api/routers/platform_onboarding.py` — register under `/platform/` prefix.
6. Add `platform:admin` role check (separate from tenant `admin` role — this is a super-admin role).
7. Cleanup task: expire `onboarding_sessions` after 7 days (cron or background task).
8. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- `platform:admin` is a JWT role claim, not a tenant role — separate authorization check.
- All resource creation (tenant, collections, users) scoped and audited.
- `draft_config` JSONB may contain quota values — not sensitive, but not exposed to tenant-level users.
- Abandoned sessions retain created resources — platform-admin must manually clean up (by design; prevents accidental data loss).
- `expires_at` on session — stale sessions auto-abandoned after 7 days.
- Activation (`step 5`) is idempotent and safe to retry if interrupted.

---

## Tests

**Unit:**
- Step 2 with duplicate collection name → idempotent update, not duplicate creation
- Step 5 without completing step 3 → 422 validation error
- `confirm_and_activate` sets `tenant.is_active = True` and writes audit log

**Integration:**
- Full wizard flow (steps 1–5) → tenant activated, collections exist, pipeline configured
- `GET /platform/onboarding/sessions/{id}` shows correct completed steps
- Tenant admin cannot access `/platform/onboarding/` (403)
- Expired session → 410 Gone on any step POST

---

## Definition of Done

- [ ] `onboarding_sessions` table + Alembic migration
- [ ] All 5 step endpoints + session GET/DELETE
- [ ] `platform:admin` role check (not tenant admin)
- [ ] Idempotent step execution (re-running same step is safe)
- [ ] Activation writes to `audit_log`
- [ ] Session expiry (7 days)
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests including full happy-path flow
- [ ] `/skill /rodo-audit` checklist: new tenant data processing basis documented
