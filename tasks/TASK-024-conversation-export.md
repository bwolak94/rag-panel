# TASK-024: Conversation Export — PDF and JSON Download

**Status:** TODO
**Priority:** P3 — user convenience; required for GDPR data portability (Art. 20)
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/prd.md`, `docs/rodo.md` §Art.20, `docs/api.md`
**Estimated effort:** 3–4 days

---

## Overview

Users need to export their conversation history for:
- **GDPR Art. 20 (data portability):** users have the right to receive their personal data in a machine-readable format.
- **Practical use:** doctors want to save a Q&A session about a procedure as a PDF for their records.

This task adds export endpoints for conversations — JSON (machine-readable, GDPR Art. 20) and PDF (human-readable, rendered with citations).

---

## Usage

**Actor:** Any authenticated user (own conversations only). Admin can export any conversation within their tenant.

**Typical scenario:**
1. User finishes a long chat session about medication protocols.
2. Clicks "Export as PDF" in the UI.
3. `POST /conversations/{id}/export?format=pdf` → returns a presigned MinIO URL (TTL 60s) to the generated PDF.
4. Browser downloads the file.

For GDPR request: `POST /users/me/export` generates a ZIP of all conversations as JSON.

---

## API Endpoints

### POST /conversations/{conversation_id}/export

**Query params:** `format=json|pdf` (default `json`)

**Response 202:**
```json
{
  "export_id": "...",
  "status": "generating",
  "format": "pdf",
  "status_url": "/api/v1/exports/{export_id}/status"
}
```

Generation is async (PDF rendering can take 1–5s). Client polls status.

### GET /exports/{export_id}/status

**Response:**
```json
{
  "export_id": "...",
  "status": "ready",
  "format": "pdf",
  "download_url": "https://minio.internal/.../export.pdf?X-Amz-Expires=60&...",
  "download_url_expires_at": "2026-08-01T10:01:00Z",
  "expires_at": "2026-08-01T11:00:00Z"
}
```

Download URL TTL: 60 seconds. Export file itself expires after 1 hour (auto-deleted from MinIO).

### POST /users/me/export (GDPR Data Portability)

Generates a ZIP containing all user's conversations in JSON format.

**Response 202:** `{ "export_id": "...", "status_url": "..." }`

---

## Export Formats

### JSON Format
```json
{
  "export_version": "1.0",
  "exported_at": "2026-08-01T10:00:00Z",
  "conversation": {
    "id": "...",
    "title": "Zapytania o procedury medyczne",
    "created_at": "...",
    "messages": [
      {
        "role": "user",
        "content": "Jakie są przeciwwskazania do stosowania ibuprofenu?",
        "created_at": "..."
      },
      {
        "role": "assistant",
        "content": "Ibuprofen jest przeciwwskazany w...",
        "sources": [
          {"document_name": "Procedury-farmakologiczne.pdf", "chunk_text": "...", "page": 12}
        ],
        "created_at": "..."
      }
    ]
  }
}
```

### PDF Format
- Header: tenant logo (if configured), conversation title, export date
- Messages in chat bubble style: user (right-aligned), assistant (left-aligned, with sources as footnotes)
- Footer: "Odpowiedzi generowane przez AI nie stanowią porady medycznej."
- Library: `weasyprint` (HTML→PDF) — render from Jinja2 HTML template

---

## Tech Stack

- **PDF:** `weasyprint` + `jinja2` template (`src/templates/conversation_export.html`)
- **JSON:** stdlib `json` — serialize conversation from DB
- **ZIP (GDPR):** stdlib `zipfile`
- **Storage:** Generated files stored in MinIO bucket `exports/{tenant_id}/{user_id}/{export_id}.{format}`, auto-expire via MinIO lifecycle policy (1h)
- **Background:** `asyncio.create_task` for generation
- **DB:** `exports` table to track export jobs

---

## Database Schema

```sql
CREATE TABLE exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    user_id UUID NOT NULL,
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    export_type VARCHAR(20) NOT NULL,   -- 'conversation' | 'gdpr_all'
    format VARCHAR(10) NOT NULL,        -- 'json' | 'pdf' | 'zip'
    status VARCHAR(20) NOT NULL DEFAULT 'generating',
    minio_key TEXT,                     -- path in MinIO (set when ready)
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL     -- now() + 1h
);
```

Alembic migration: `0013_exports.py`

---

## Implementation Steps

1. Alembic migration: `exports` table.
2. `src/domain/export_service.py`:
   - `create_conversation_export(conversation_id, format, ctx) -> Export`
   - `create_gdpr_export(ctx) -> Export`
   - `get_export_status(export_id, ctx) -> Export`
   - `_generate_pdf(conversation) -> bytes` — render Jinja2 template → weasyprint
   - `_generate_json(conversation) -> bytes`
3. Create `src/templates/conversation_export.html` (Jinja2).
4. Router `src/api/routers/exports.py`.
5. MinIO lifecycle policy for `exports/` bucket prefix: expire after 1h.
6. Authorize: user can only export own conversations; admin can export any in their tenant.
7. Update `docs/03-Specyfikacja-API.md`.

---

## Security

- Export file stored in MinIO with private access; presigned URL TTL 60s.
- Presigned URL not logged (contains credentials).
- Export expires after 1h — MinIO lifecycle policy enforced at object level.
- GDPR export: user can only export their own data (`user_id` from JWT, never from body).
- Admin exporting another user's conversation is logged in `audit_log`.
- PDF template sanitizes all user-generated content (Jinja2 auto-escape).

---

## Tests

**Unit:**
- `_generate_pdf` produces valid PDF bytes for a fixture conversation
- JSON export matches expected schema
- Export expires_at is set to `now() + 1h`

**Integration:**
- `POST /conversations/{id}/export?format=json` → status `ready` → download URL works
- User cannot export another user's conversation (403)
- Admin can export any conversation in their tenant
- Cross-tenant: admin A cannot export conversation from tenant B (404)

---

## Definition of Done

- [ ] `exports` table + Alembic migration
- [ ] JSON and PDF export for conversations
- [ ] GDPR all-conversations ZIP export (`POST /users/me/export`)
- [ ] MinIO lifecycle policy for auto-expiry (1h)
- [ ] Presigned URL TTL = 60s
- [ ] Audit log entry when admin exports another user's conversation
- [ ] Jinja2 PDF template with medical disclaimer footer
- [ ] `docs/03-Specyfikacja-API.md` updated
- [ ] Unit + integration tests
- [ ] `/skill /rodo-audit` checklist: GDPR Art. 20 data portability verified
