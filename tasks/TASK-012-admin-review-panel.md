# TASK-012: Panel administratora — weryfikacja dokumentow (needs_review)

**Status:** TODO
**Priorytet:** P1 — blokuje indeksowanie dokumentow wymagajacych recenzji
**Wlasciciel:** backend-dev + design
**Recenzent:** python-reviewer (profil security) + security-auditor
**Powiazane dokumenty:** `docs/architecture.md` §6, `docs/data-model.md` §2.3, `docs/03-Specyfikacja-API.md`
**Szacowany naklad:** 5–7 dni

---

## Overview

Gdy graf ingestowy wykryje PII lub uzyska niska pewnosc klasyfikacji dokumentu (quality_score ponizej progu z `collections.validation_config`), ustawia `documents.status = needs_review` i zatrzymuje graf z zapisanym checkpointem w Postgres. Od tej chwili dokument czeka na decyzje czlowieka.

Zadanie obejmuje:

1. Zestaw endpointow REST w przestrzeni `/admin/` umozliwiajacych administratorowi przeglad kolejki, podglad szczegolow walidacji i podjecie decyzji (zatwierdz / odrzuc).
2. Specyfikacje UX panelu recenzji (text wireframe + stany UI) gotowa do implementacji przez frontend lub Open WebUI custom panel.
3. Perelemenowa specyfikacje schematow Pydantic i odpowiedzi HTTP zgodna z reszta API (`docs/03-Specyfikacja-API.md`).

Kontekst biznesowy: pilot to przychodnia medyczna. Dokumenty zawieraja procedury medyczne i dane pacjentow. Blad w tej warstwie moze oznaczac indeksowanie dokumentu z PII — naruszenie RODO. Dlatego podejscie domyslne jest konserwatywne: watpliwy dokument idzie do kolejki, nie do indeksu.

---

## Usage

**Aktorzy:**

| Rola | Uprawnienie | Opis |
|---|---|---|
| Owner | `documents:review` | Moze przegladac i podejmowac decyzje dla calego tenanta |
| Admin | `documents:review` | Moze przegladac i podejmowac decyzje dla kolekcji, do ktorej ma dostep |
| Contributor | brak | Widzi status swojego dokumentu, nie widzi kolejki recenzji |
| Viewer | brak | Brak dostepu do panelu admina |

**Typowy scenariusz:**

1. Pracownik (Contributor) uploaduje PDF z procedura medyczna.
2. Graf ingestowy wykrywa potencjalne PII (numer PESEL w tekscie) — status `needs_review`.
3. Admin loguje sie, widzi badge `3` przy pozycji "Dokumenty do recenzji" w nawigacji.
4. Otwiera kolejke, filtruje po kolekcji "Procedury medyczne".
5. Klika "Przejrzyj" przy podejrzanym dokumencie.
6. Widzi breakdown walidacji: `pii_flags: ["PESEL_NUMBER"]`, `quality_score: 0.61`, `detected_type: "discharge_summary"`.
7. Oglada podglad PDF (presigned URL z MinIO, TTL 5 min).
8. Decyduje: odrzuca dokument z powodem "Dokument zawiera dane osobowe pacjenta — nalezy go zanonimizowac przed ponownym wyslaniem".
9. System zapisuje wpis do audit_log, powiadamia uploadera.

---

## Tech Stack

- **Framework:** FastAPI, async handlers
- **Autoryzacja:** `Depends(require_permission("documents:review"))` — dependency z `src/api/dependencies/auth.py`
- **Baza danych:** SQLAlchemy async ORM, PostgreSQL 16
- **MinIO:** `minio.presigned_get_object()` — TTL 300 sekund (5 minut), nie wiecej (zgodnie z regula bezpieczenstwa)
- **Redis Streams:** `XADD ingest_events` przy zatwierdzeniu (wznowienie ingestu)
- **LangGraph:** Wznowienie grafu z zapisanego checkpointa (Postgres checkpointer) — wola ingest workera, nie API bezposrednio
- **Audit:** `AuditService.log()` dla kazdej decyzji approve/reject
- **Powiadomienia:** Opcjonalne (MVP: brak push notifications; audit log + status dokumentu jako mechanizm zwrotny dla uploadera)

---

## Database Patterns

### Tabele uzywane przez ten task

**`documents`** — glowna tabela. Filtrujemy po `status = 'needs_review'` AND `tenant_id = ctx.tenant_id`.

Kluczowe kolumny:
- `validation_result JSONB` — szczegoly walidacji z grafu. Schemat:

```json
{
  "detected_type": "discharge_summary",
  "category": "patient_record",
  "quality_score": 0.61,
  "confidence": 0.72,
  "pii_flags": ["PESEL_NUMBER", "PATIENT_NAME"],
  "issues": [
    {"code": "LOW_QUALITY", "message": "Dokument ponizej progu jakosci (0.70)"},
    {"code": "PII_DETECTED", "message": "Wykryto potencjalne dane osobowe: PESEL_NUMBER"}
  ],
  "llm_raw_response": null
}
```

Uwaga: `llm_raw_response` nigdy nie trafia do logow (regula security). Moze byc zapisany w JSONB dla potrzeb debugowania, ale nie jest eksponowany przez API.

- `reviewed_by UUID` — FK do `users.id`, uzupelniony po decyzji
- `reviewed_at TIMESTAMPTZ` — znacznik czasu decyzji

**`ingestion_jobs`** — historia krokow przetwarzania dokumentu.

Kolumna `steps JSONB` to tablica obiektow:

```json
[
  {"stage": "fetch_from_minio", "status": "completed", "started_at": "...", "completed_at": "...", "error": null, "meta": {}},
  {"stage": "extract_text", "status": "completed", "started_at": "...", "completed_at": "...", "error": null, "meta": {"page_count": 12}},
  {"stage": "dedupe_check", "status": "completed", "started_at": "...", "completed_at": "...", "error": null, "meta": {}},
  {"stage": "llm_validate", "status": "completed", "started_at": "...", "completed_at": "...", "error": null, "meta": {"model_used": "mistral-7b"}},
  {"stage": "pii_scan", "status": "awaiting_review", "started_at": "...", "completed_at": null, "error": null, "meta": {"flags_count": 2}}
]
```

**`audit_log`** — kazda decyzja approve/reject jest wpisem z:
- `action`: `"document.approved"` lub `"document.rejected"`
- `resource_type`: `"document"`
- `resource_id`: `document_id`
- `details JSONB`: `{"reason": "...", "previous_status": "needs_review", "reviewer_role": "Admin"}`

### Zapytania SQL (wzorce)

```sql
-- Kolejka recenzji (z paginacja cursor-based)
SELECT d.id, d.original_filename, d.collection_id, d.uploaded_by,
       d.created_at, d.validation_result, c.name AS collection_name,
       u.display_name AS uploader_name
FROM documents d
JOIN collections c ON c.id = d.collection_id
JOIN users u ON u.id = d.uploaded_by
WHERE d.tenant_id = :tenant_id
  AND d.status = 'needs_review'
  AND (:collection_id IS NULL OR d.collection_id = :collection_id)
  AND (:cursor IS NULL OR d.created_at < :cursor_ts OR
       (d.created_at = :cursor_ts AND d.id < :cursor_id))
ORDER BY d.created_at DESC, d.id DESC
LIMIT :page_size + 1;  -- +1 dla wykrycia hasNextPage
```

Cursor-based paginacja: cursor = base64(created_at + ":" + id). Nalezy uzywac LIMIT N+1 do wykrycia `has_next_page`.

---

## API Contracts

Wszystkie endpointy sa pod prefixem `/api/v1`. Wymagaja naglowka `Authorization: Bearer <JWT>`.
Odpowiedzi bledow zgodne z RFC 7807 Problem Details.

### Schematy Pydantic

```python
# src/api/schemas/admin_review.py

from __future__ import annotations
from uuid import UUID
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class ValidationIssue(BaseModel):
    code: str = Field(..., description="Kod bledu: LOW_QUALITY | PII_DETECTED | UNKNOWN_TYPE | LOW_CONFIDENCE")
    message: str = Field(..., description="Opis problemu w jezyku polskim, gotowy do wyswietlenia w UI")


class ValidationResult(BaseModel):
    detected_type: str | None = Field(None, description="Wykryty typ dokumentu")
    category: str | None = Field(None, description="Kategoria dokumentu")
    quality_score: float | None = Field(None, ge=0.0, le=1.0)
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    pii_flags: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)


class IngestionStep(BaseModel):
    stage: str
    status: str  # pending | running | completed | failed | awaiting_review
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    meta: dict[str, Any] = Field(default_factory=dict)


class ReviewQueueItem(BaseModel):
    id: UUID
    filename: str
    collection_id: UUID
    collection_name: str
    uploaded_by_id: UUID
    uploaded_by_name: str
    uploaded_at: datetime
    validation_result: ValidationResult
    # Skrocony opis powodu flagi — gotowy do wyswietlenia w tabeli
    flag_summary: str = Field(..., description="Np. 'PII: PESEL_NUMBER, LOW_QUALITY'")


class ReviewQueueResponse(BaseModel):
    items: list[ReviewQueueItem]
    total_count: int
    next_cursor: str | None = None
    has_next_page: bool


class DocumentReviewDetail(BaseModel):
    id: UUID
    filename: str
    collection_id: UUID
    collection_name: str
    uploaded_by_id: UUID
    uploaded_by_name: str
    uploaded_at: datetime
    size_bytes: int
    mime_type: str
    validation_result: ValidationResult
    ingestion_steps: list[IngestionStep]
    ingestion_job_id: UUID
    preview_url: str = Field(..., description="Presigned MinIO GET URL, TTL=300s")
    preview_url_expires_at: datetime


class ApproveDocumentRequest(BaseModel):
    note: str | None = Field(
        None,
        max_length=500,
        description="Opcjonalna notatka administratora (trafia do audit_log)"
    )


class RejectDocumentRequest(BaseModel):
    reason: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="Powod odrzucenia — wymagany; wyswietlany uplodujacemu"
    )


class ReviewDecisionResponse(BaseModel):
    document_id: UUID
    new_status: str  # 'queued' | 'rejected'
    decided_at: datetime
    decided_by_id: UUID
    message: str  # Komunikat po polsku dla UI
```

---

### Endpoint 1: GET /admin/documents/review-queue

**Opis:** Lista dokumentow oczekujacych na recenzje w ramach tenanta.

**Uprawnienie:** `documents:review`

**Parametry zapytania:**

| Parametr | Typ | Opis |
|---|---|---|
| `collection_id` | UUID (opcjonalny) | Filtruj po kolekcji |
| `cursor` | string (opcjonalny) | Cursor z poprzedniej strony |
| `page_size` | int (opcjonalny, default 20, max 100) | Liczba wynikow |

**Przykladowa odpowiedz 200:**

```json
{
  "items": [
    {
      "id": "d1e2f3a4-...",
      "filename": "procedura-wypisania-pacjenta.pdf",
      "collection_id": "a1b2c3d4-...",
      "collection_name": "Procedury medyczne",
      "uploaded_by_id": "u1u2u3u4-...",
      "uploaded_by_name": "Anna Kowalska",
      "uploaded_at": "2026-07-13T10:22:00Z",
      "validation_result": {
        "detected_type": "discharge_summary",
        "category": "patient_record",
        "quality_score": 0.61,
        "confidence": 0.72,
        "pii_flags": ["PESEL_NUMBER"],
        "issues": [
          {"code": "PII_DETECTED", "message": "Wykryto potencjalne dane osobowe: numer PESEL"},
          {"code": "LOW_QUALITY", "message": "Wynik jakosci dokumentu ponizej wymaganego progu (0.70)"}
        ]
      },
      "flag_summary": "PII: PESEL_NUMBER · Niska jakosc (0.61)"
    }
  ],
  "total_count": 3,
  "next_cursor": "eyJjcmVhdGVkX2F0IjogIjIwMjYtMDctMTNUMTA6MjI6MDBaIiwgImlkIjogImQxZTJmM2E0In0=",
  "has_next_page": false
}
```

**Odpowiedzi bledow:**

| Kod | Warunek | Tresc |
|---|---|---|
| 401 | Brak lub niepoprawny JWT | `{"type": "about:blank", "title": "Brak autoryzacji", "status": 401, "detail": "Zaloguj sie, aby uzyskac dostep."}` |
| 403 | Rola bez uprawnienia `documents:review` | `{"type": "about:blank", "title": "Brak uprawnien", "status": 403, "detail": "Nie masz uprawnien do przegladu kolejki recenzji. Skontaktuj sie z administratorem."}` |

---

### Endpoint 2: GET /admin/documents/{id}/review

**Opis:** Szczegoly dokumentu do recenzji: wyniki walidacji, kroki ingestowe, presigned URL do podgladu.

**Uprawnienie:** `documents:review` + weryfikacja `document.tenant_id == ctx.tenant_id`

**Przykladowa odpowiedz 200:**

```json
{
  "id": "d1e2f3a4-...",
  "filename": "procedura-wypisania-pacjenta.pdf",
  "collection_id": "a1b2c3d4-...",
  "collection_name": "Procedury medyczne",
  "uploaded_by_id": "u1u2u3u4-...",
  "uploaded_by_name": "Anna Kowalska",
  "uploaded_at": "2026-07-13T10:22:00Z",
  "size_bytes": 2457600,
  "mime_type": "application/pdf",
  "validation_result": {
    "detected_type": "discharge_summary",
    "category": "patient_record",
    "quality_score": 0.61,
    "confidence": 0.72,
    "pii_flags": ["PESEL_NUMBER"],
    "issues": [
      {"code": "PII_DETECTED", "message": "Wykryto potencjalne dane osobowe: numer PESEL"},
      {"code": "LOW_QUALITY", "message": "Wynik jakosci dokumentu ponizej wymaganego progu (0.70)"}
    ]
  },
  "ingestion_steps": [
    {"stage": "fetch_from_minio", "status": "completed", "started_at": "2026-07-13T10:22:05Z", "completed_at": "2026-07-13T10:22:06Z", "error": null, "meta": {}},
    {"stage": "extract_text", "status": "completed", "started_at": "2026-07-13T10:22:06Z", "completed_at": "2026-07-13T10:22:18Z", "error": null, "meta": {"page_count": 12}},
    {"stage": "dedupe_check", "status": "completed", "started_at": "2026-07-13T10:22:18Z", "completed_at": "2026-07-13T10:22:18Z", "error": null, "meta": {}},
    {"stage": "llm_validate", "status": "completed", "started_at": "2026-07-13T10:22:18Z", "completed_at": "2026-07-13T10:22:31Z", "error": null, "meta": {"model_used": "mistral-7b"}},
    {"stage": "pii_scan", "status": "awaiting_review", "started_at": "2026-07-13T10:22:31Z", "completed_at": null, "error": null, "meta": {"flags_count": 1}}
  ],
  "ingestion_job_id": "j1j2j3j4-...",
  "preview_url": "https://minio.internal/tenant-klinika/raw/a1b2c3d4/d1e2f3a4/procedura.pdf?X-Amz-Expires=300&...",
  "preview_url_expires_at": "2026-07-13T10:32:00Z"
}
```

**Odpowiedzi bledow:**

| Kod | Warunek | Tresc |
|---|---|---|
| 404 | Dokument nie istnieje lub nie nalezy do tenanta | `{"title": "Nie znaleziono", "status": 404, "detail": "Dokument o podanym identyfikatorze nie istnieje lub nie masz do niego dostepu."}` |
| 409 | Dokument nie jest w statusie `needs_review` | `{"title": "Nieprawidlowy stan", "status": 409, "detail": "Ten dokument nie oczekuje na recenzje. Aktualny status: completed."}` |

---

### Endpoint 3: POST /admin/documents/{id}/approve

**Opis:** Zatwierdza dokument — ustawia `status=queued` i publikuje zdarzenie do Redis Streams, aby wznowic ingest.

**Uprawnienie:** `documents:review`

**Cialo zadania:**

```json
{
  "note": "Dokument zatwierdzony po weryfikacji — PII to dane przykladowe, nie rzeczywiste."
}
```

**Przykladowa odpowiedz 200:**

```json
{
  "document_id": "d1e2f3a4-...",
  "new_status": "queued",
  "decided_at": "2026-07-13T11:05:00Z",
  "decided_by_id": "admin-user-id-...",
  "message": "Dokument zostal zatwierdzony i wroci do kolejki przetwarzania."
}
```

**Logika backendowa (specyfikacja dla backend-dev):**

```
1. Pobierz dokument; weryfikuj tenant_id == ctx.tenant_id (404 jesli nie)
2. Weryfikuj status == 'needs_review' (409 jesli inny)
3. W transakcji Postgres:
   a. UPDATE documents SET status='queued', reviewed_by=ctx.user_id, reviewed_at=now() WHERE id=:id
   b. UPDATE ingestion_jobs SET status='queued' WHERE document_id=:id AND status='awaiting_review'
   c. INSERT audit_log (action='document.approved', resource_type='document', resource_id=:id,
      details={"note": ..., "previous_status": "needs_review", "reviewer_role": ctx.highest_role})
4. XADD ingest_events * document_id :id tenant_id :tenant_id action "resume"
   (Worker pobierze zdarzenie i wznowi graf ingestowy z checkpointa)
5. Zwroc ReviewDecisionResponse
```

**Odpowiedzi bledow:**

| Kod | Warunek |
|---|---|
| 404 | Dokument nieznaleziony w tenacie |
| 409 | Status != 'needs_review' |
| 500 | Blad publikacji do Redis (nalezy logowac jako CRITICAL; transaction rollback) |

---

### Endpoint 4: POST /admin/documents/{id}/reject

**Opis:** Odrzuca dokument — ustawia `status=rejected`. Wymaga podania powodu.

**Uprawnienie:** `documents:review`

**Cialo zadania:**

```json
{
  "reason": "Dokument zawiera dane osobowe pacjenta (numer PESEL). Nalezy go zanonimizowac przed ponownym wyslaniem."
}
```

**Przykladowa odpowiedz 200:**

```json
{
  "document_id": "d1e2f3a4-...",
  "new_status": "rejected",
  "decided_at": "2026-07-13T11:10:00Z",
  "decided_by_id": "admin-user-id-...",
  "message": "Dokument zostal odrzucony. Uplodujacy zostanie poinformowany o powodzie odrzucenia."
}
```

**Logika backendowa:**

```
1. Pobierz dokument; weryfikuj tenant_id (404)
2. Weryfikuj status == 'needs_review' (409)
3. W transakcji:
   a. UPDATE documents SET status='rejected', reviewed_by=ctx.user_id, reviewed_at=now(),
      validation_result = validation_result || '{"rejection_reason": "<reason>"}'::jsonb
   b. UPDATE ingestion_jobs SET status='rejected' WHERE document_id=:id
   c. INSERT audit_log (action='document.rejected', details={"reason": ..., "reviewer_role": ...})
4. [Opcjonalnie MVP+] Powiadom uploadera: INSERT INTO notifications lub wyslij email
5. Zwroc ReviewDecisionResponse
```

---

### Endpoint 5: GET /admin/ingestion-jobs/{id}

**Opis:** Pelne szczegoly zadania ingestowego — do debugowania przez admina.

**Uprawnienie:** `documents:review`

**Przykladowa odpowiedz 200:**

```json
{
  "id": "j1j2j3j4-...",
  "document_id": "d1e2f3a4-...",
  "status": "awaiting_review",
  "current_step": "pii_scan",
  "retry_count": 0,
  "langgraph_thread_id": "lg-thread-...",
  "steps": [...],
  "started_at": "2026-07-13T10:22:05Z",
  "completed_at": null,
  "created_at": "2026-07-13T10:22:04Z"
}
```

---

## UX Flow & Wireframe (text-based)

### Stan 1: Pusta kolejka

```
+----------------------------------------------------------+
| Panel administratora > Dokumenty do recenzji            |
+----------------------------------------------------------+
|                                                          |
|          [ikona dokumentu — brak elementow]              |
|                                                          |
|     Brak dokumentow do recenzji                          |
|     Wszystkie dokumenty zostaly przetworzone.            |
|                                                          |
+----------------------------------------------------------+
```

### Stan 2: Ladowanie

```
+----------------------------------------------------------+
| Panel administratora > Dokumenty do recenzji    [3]     |
+----------------------------------------------------------+
| Kolekcja: [Wszystkie kolekcje v]                         |
+----------------------------------------------------------+
| Ladowanie kolejki...                                     |
| [==============================] (skeleton loader)       |
| [==============================]                         |
| [==============================]                         |
+----------------------------------------------------------+
```

### Stan 3: Kolejka recenzji — widok tabeli

```
+------------------------------------------------------------------+
| Panel administratora > Dokumenty do recenzji              [3]   |
+------------------------------------------------------------------+
| Filtruj:  Kolekcja: [Procedury medyczne v]                       |
+------------------------------------------------------------------+
| Plik                    | Kolekcja           | Uplodujacy        |
|                         |                    |                   |
| Data uploadu            | Powod flagi        | Akcja             |
+------------------------------------------------------------------+
| procedura-wypisania.pdf | Procedury medyczne | Anna Kowalska     |
| 13.07.2026, 10:22       | PII: PESEL · Niska | [Przejrzyj]       |
|                         | jakosc (0.61)      |                   |
+------------------------------------------------------------------+
| skierowanie-2026.pdf    | Skierowania        | Jan Nowak         |
| 13.07.2026, 09:15       | Nieznany typ doc.  | [Przejrzyj]       |
+------------------------------------------------------------------+
| regulamin-oddzialu.pdf  | Dokumenty wewn.    | Maria Wisniewska  |
| 12.07.2026, 16:40       | Niska pewnosc      | [Przejrzyj]       |
|                         | (0.58)             |                   |
+------------------------------------------------------------------+
|                              Lacznie: 3 dokumenty               |
+------------------------------------------------------------------+
```

Kolumna "Powod flagi" — skrocony, czytelny opis z `flag_summary`.
Przycisk "Przejrzyj" nawiguje do widoku szczegolowego dokumentu.

### Stan 4: Widok szczegolowy dokumentu

```
+------------------------------------------------------------------+
| < Wstecz do kolejki                                              |
| Recenzja dokumentu: procedura-wypisania-pacjenta.pdf            |
+------------------------------------------------------------------+
|                                                                  |
| INFORMACJE O DOKUMENCIE                                          |
| Kolekcja:     Procedury medyczne                                 |
| Uplodujacy:   Anna Kowalska                                      |
| Data uploadu: 13 lipca 2026, godz. 10:22                        |
| Rozmiar:      2,4 MB                                             |
| Typ MIME:     application/pdf                                    |
|                                                                  |
+------------------------------------------------------------------+
| WYNIKI WALIDACJI                                                 |
+------------------------------------------------------------------+
| Wykryty typ:     Karta wypisu pacjenta                           |
| Kategoria:       Dokumentacja medyczna                           |
| Wynik jakosci:   0.61 / 1.00  [====------]  (PONIZEJ PROGU)     |
| Pewnosc:         0.72 / 1.00  [=======---]                       |
|                                                                  |
| Problemy:                                                        |
|  [!] Wykryto potencjalne dane osobowe: numer PESEL               |
|  [!] Wynik jakosci ponizej wymaganego progu (0.70)               |
+------------------------------------------------------------------+
| ETAPY PRZETWARZANIA                                              |
+------------------------------------------------------------------+
|  [v] Pobranie pliku              — ukonczono 10:22:06           |
|  [v] Ekstrakcja tekstu           — ukonczono 10:22:18 (12 str)  |
|  [v] Sprawdzenie duplikatow      — ukonczono 10:22:18           |
|  [v] Walidacja LLM               — ukonczono 10:22:31           |
|  [~] Skanowanie PII              — oczekuje na recenzje          |
|  [ ] Podzial na fragmenty        — oczekuje                      |
|  [ ] Generowanie osadzen         — oczekuje                      |
|  [ ] Zapis do bazy wektorowej    — oczekuje                      |
+------------------------------------------------------------------+
| PODGLAD DOKUMENTU                                                |
+------------------------------------------------------------------+
|                                                                  |
| [Iframe z podgladem PDF — presigned URL, TTL 5 min]             |
|                                                                  |
|  Podglad wygasa o: 10:32:00  [Odswierz podglad]                 |
+------------------------------------------------------------------+
| DECYZJA                                                          |
+------------------------------------------------------------------+
|                                                                  |
|  [Zatwierdz dokument]          [Odrzuc dokument]                 |
|                                                                  |
+------------------------------------------------------------------+
```

### Stan 5: Modal zatwierdzenia

```
+----------------------------------------+
| Potwierdzenie zatwierdzenia            |
+----------------------------------------+
| Czy na pewno chcesz zatwierdzic ten   |
| dokument do przetwarzania?             |
|                                        |
| Dokument zostanie dodany do kolejki    |
| i zaindeksowany w bazie wiedzy.        |
|                                        |
| Notatka (opcjonalna):                  |
| [_________________________________]   |
|                                        |
| [Anuluj]    [Tak, zatwierdz]           |
+----------------------------------------+
```

### Stan 6: Modal odrzucenia

```
+----------------------------------------+
| Odrzucenie dokumentu                   |
+----------------------------------------+
| Podaj powod odrzucenia. Informacja     |
| trafi do historii zdarzen i moze byc  |
| wyswietlona uplodujacemu.              |
|                                        |
| Powod odrzucenia: *                    |
| [_________________________________]   |
| [_________________________________]   |
|                                        |
| * Pole wymagane (min. 10 znakow)       |
|                                        |
| [Anuluj]    [Odrzuc dokument]          |
+----------------------------------------+
```

### Stan 7: Toast sukcesu (zatwierdzenie)

```
+--------------------------------------------------+
| [v] Dokument zatwierdzony                        |
|     Dokument wroci do kolejki przetwarzania.     |
+--------------------------------------------------+
```

### Stan 8: Toast sukcesu (odrzucenie)

```
+--------------------------------------------------+
| [v] Dokument odrzucony                           |
|     Uplodujacy zostanie poinformowany.           |
+--------------------------------------------------+
```

### Stan 9: Blad systemu

```
+--------------------------------------------------+
| [x] Nie mozna przetworzyc decyzji                |
|     Sprobuj ponownie. Jesli problem powtorza     |
|     sie, skontaktuj sie z administratorem        |
|     systemu (kod: ERR-REVIEW-503).               |
+--------------------------------------------------+
```

### Odznaka nawigacyjna

Pozycja w menu "Dokumenty do recenzji" powinna wyswietlac liczbe oczekujacych dokumentow.
Wartosc pochodzi z `GET /admin/documents/review-queue?page_size=1` (pole `total_count`).
Frontend odpytuje co 60 sekund lub po kazdej akcji recenzji.

```
Dokumenty do recenzji  [3]   <-- odznaka z liczba
```

Jesl `total_count == 0`, odznaka nie jest wyswietlana.

---

## Architecture — SOLID & DRY

### Warstwa API (`src/api/routers/admin_review.py`)

Router eksponuje endpointy. Nie zawiera logiki biznesowej. Deleguje do `AdminReviewService`.

```python
# Sygnatury (nie implementacja)
router = APIRouter(prefix="/admin", tags=["admin-review"])

@router.get("/documents/review-queue", response_model=ReviewQueueResponse)
async def get_review_queue(
    collection_id: UUID | None = None,
    cursor: str | None = None,
    page_size: int = Query(20, ge=1, le=100),
    ctx: UserContext = Depends(require_permission("documents:review")),
    service: AdminReviewService = Depends(get_admin_review_service),
) -> ReviewQueueResponse: ...

@router.get("/documents/{document_id}/review", response_model=DocumentReviewDetail)
async def get_document_review_detail(
    document_id: UUID,
    ctx: UserContext = Depends(require_permission("documents:review")),
    service: AdminReviewService = Depends(get_admin_review_service),
) -> DocumentReviewDetail: ...

@router.post("/documents/{document_id}/approve", response_model=ReviewDecisionResponse)
async def approve_document(
    document_id: UUID,
    body: ApproveDocumentRequest,
    ctx: UserContext = Depends(require_permission("documents:review")),
    service: AdminReviewService = Depends(get_admin_review_service),
) -> ReviewDecisionResponse: ...

@router.post("/documents/{document_id}/reject", response_model=ReviewDecisionResponse)
async def reject_document(
    document_id: UUID,
    body: RejectDocumentRequest,
    ctx: UserContext = Depends(require_permission("documents:review")),
    service: AdminReviewService = Depends(get_admin_review_service),
) -> ReviewDecisionResponse: ...

@router.get("/ingestion-jobs/{job_id}", response_model=IngestionJobDetail)
async def get_ingestion_job(
    job_id: UUID,
    ctx: UserContext = Depends(require_permission("documents:review")),
    service: AdminReviewService = Depends(get_admin_review_service),
) -> IngestionJobDetail: ...
```

### Warstwa domenowa (`src/domain/admin_review_service.py`)

`AdminReviewService` — jeden obiekt, wstrzykliwalne zaleznosci:

- `DocumentRepository` — SQL, only reads/writes `documents` table
- `IngestionJobRepository` — SQL, reads `ingestion_jobs`
- `AuditService` — zapis do `audit_log`
- `MinIOClient` — generowanie presigned URLs
- `RedisStreamPublisher` — publikacja zdarzen ingestowych

Serwis nie importuje niczego z `src/api/`.

### Separacja odpowiedzialnosci

| Komponent | Odpowiedzialnosc |
|---|---|
| Router | Walidacja wejscia Pydantic, autoryzacja (Depends), delegacja do service |
| AdminReviewService | Logika biznesowa: weryfikacja stanu, transakcja, audit |
| DocumentRepository | SQL dla tabeli documents; enkapsuluje filtry tenant_id |
| AuditService | Zapis audit_log; nie wywolywany bezposrednio z routera |
| MinIOClient | Presigned URL; TTL egzekwowany tu, nie w routerze |
| RedisStreamPublisher | XADD; blad = wyjatek, obsluzona w service (rollback transakcji) |

---

## Implementation Steps

1. **Schematy Pydantic** — stworz `src/api/schemas/admin_review.py` ze wszystkimi modelami z sekcji "API Contracts".

2. **Repozytorium dokumentow** — dodaj do `DocumentRepository`:
   - `get_needs_review_queue(tenant_id, collection_id, cursor, page_size) -> tuple[list[Document], bool]`
   - `get_document_for_review(tenant_id, document_id) -> Document | None`
   - `set_document_approved(tenant_id, document_id, reviewer_id, note) -> Document` (w transakcji)
   - `set_document_rejected(tenant_id, document_id, reviewer_id, reason) -> Document` (w transakcji)

3. **Repozytorium ingestion_jobs** — dodaj `get_by_document_id(tenant_id, document_id) -> IngestionJob | None`.

4. **AdminReviewService** — stworz `src/domain/admin_review_service.py`:
   - Implementuj metody dla kazdego endpointu
   - Enkapsuluj logike cursor-based pagination (encode/decode base64)
   - Wywolaj `AuditService.log()` przy approve/reject (nie pozwol na ominiecje)
   - Wywolaj `RedisStreamPublisher.publish_ingest_event()` przy approve

5. **MinIO presigned URL** — w `AdminReviewService.get_document_review_detail()`:
   - Wywolaj `minio_client.presigned_get_object(bucket, key, expires=300)`
   - Wylicz `preview_url_expires_at = now() + timedelta(seconds=300)`
   - NIE cache'uj URL — kazde wywolanie generuje nowy (TTL per-request)

6. **Router** — stworz `src/api/routers/admin_review.py`, zarejestruj w `src/api/main.py`.

7. **Mapowanie wyjatkow** — w `src/core/exceptions.py` dodaj jesli brakuje:
   - `DocumentNotFoundError` → 404
   - `InvalidDocumentStateError` → 409 z aktualnym statusem w `detail`

8. **Wpis do docs/03-Specyfikacja-API.md** — wymagany przed mergem (regula projektu).

9. **Uprawnienie** — sprawdz czy `documents:review` istnieje w tabeli `permissions`; jesli nie, napisz migracjê Alembic dodajaca wpis i przypisujaca do rol Admin i Owner.

---

## Security Checklist

- [ ] Kazdy endpoint weryfikuje `document.tenant_id == ctx.tenant_id` — nie tylko przez SQL WHERE, ale rowniez jawnie w serwisie (resource-level authorization, nie tylko endpoint-level)
- [ ] `tenant_id` pochodzi wylacznie z JWT (ctx.tenant_id) — nigdy z body ani query param
- [ ] Presigned URL: TTL <= 300 sekund (5 minut); URL nie jest logowany (zawiera credentials)
- [ ] `llm_raw_response` z `validation_result` nie jest eksponowany przez API ani logowany
- [ ] Audit log: kazda decyzja approve/reject jest zapisana przed zwroceniem odpowiedzi HTTP
- [ ] Redis publish: blad publikacji powoduje rollback transakcji Postgres (brak "ghost approval" bez wznowienia ingestu)
- [ ] `reason` przy odrzuceniu jest wlaczony do audit_log, ale NIE do logow aplikacji (moze zawierac tresci dokumentu)
- [ ] Rate limiting na endpointach POST (approve/reject): maks. 30 req/min per user (konfiguracja Nginx/FastAPI Limiter)
- [ ] CORS: endpointy `/admin/` dostepne tylko z dozwolonych origin (konfiguracja per-tenant lub globalna)
- [ ] Test tenant isolation: approve/reject dokumentu z innego tenanta musi zwracac 404 (nie 403 — nie ujawniamy istnienia zasobu)

---

## Terms of Use (relevant constraints)

- Przetwarzanie danych w panelu recenzji jest objete RODO Art. 6 ust. 1 lit. c (obowiazek prawny) i Art. 9 (dane szczegolnych kategorii — medyczne).
- Administrator dokonujacy recenzji uzyskuje wglad w tresc dokumentu (podglad PDF). Nalezy to odnotowac w rejestrze czynnosci przetwarzania (RCP).
- Presigned URL daje dostep do surowego pliku w MinIO — nalezy zapewnic, ze linki nie sa udostepniane poza systemem (np. przez copy-paste).
- Powod odrzucenia zapisany w audit_log jest daną osobowa, jesli zawiera informacje identyfikujace (np. "dokument nalezy do Jana Kowalskiego"). Retencja audit_log zgodna z `tenants.settings.retention_days`.
- System nie jest uprawniony do podejmowania automatycznych decyzji opartych na danych wrażliwych (Art. 22 RODO) — dlatego decyzja approve/reject musi byc akcja czlowieka; brak autoapprovalu.

---

## Open WebUI Integration Note

Panel recenzji NIE jest czescia Open WebUI. Open WebUI to frontend do chatowania (klient OpenAI-compatible API). Panel admina to oddzielna aplikacja.

**Opcja A (rekomendowana dla MVP):** Oddzielna aplikacja frontendowa (np. React/Vue SPA) serwowana pod `/admin/` przez ten sam reverse proxy (Nginx). Autentykacja przez ten sam Keycloak OIDC. Komunikuje sie z endpointami `/api/v1/admin/`.

**Opcja B (dla ograniczonego budzetowego MVP):** Strona administracyjna Open WebUI (`/admin/settings`) moze zawierac link zewnetrzny do panelu recenzji albo iframe. Nie jest mozliwe natywne osadzenie custom widgetow w Open WebUI bez modyfikacji kodu zrodlowego (co narusza warunki licencji community jezeli MAU > 50).

**Odznaka nawigacyjna** (liczba dokumentow do recenzji): jesli frontend jest osadzony w Open WebUI custom HTML injection (dostepne w ustawieniach) — nalezy sprawdzic czy Open WebUI pozwala na custom JS polling. W MVP: adminowi wyswietlamy liczbe w oddzielnym panelu.

---

## Tests

Kazdy test musi byc oznaczony odpowiednimi markerami pytest. Testy bezpieczenstwa z `@pytest.mark.tenant_isolation` sa blokerem merge.

### Testy jednostkowe

**`tests/unit/admin_review/test_review_service.py`**

```python
# Wymagane przypadki testowe:

# 1. get_review_queue — zwraca tylko dokumenty z tenant_id z kontekstu
# 2. get_review_queue — filtruje po collection_id
# 3. get_review_queue — paginacja cursor-based (has_next_page=True gdy > page_size)
# 4. get_document_review_detail — generuje presigned URL z TTL=300s
# 5. approve_document — ustawia status='queued', reviewer_id, reviewed_at
# 6. approve_document — wywoluje AuditService.log z action='document.approved'
# 7. approve_document — publikuje zdarzenie do Redis Streams
# 8. approve_document — rollback Postgres jesli Redis publish failuje
# 9. reject_document — ustawia status='rejected', zapisuje reason w validation_result
# 10. reject_document — wywoluje AuditService.log z action='document.rejected'
# 11. approve_document — rzuca InvalidDocumentStateError jesli status != 'needs_review'
# 12. reject_document — rzuca InvalidDocumentStateError jesli status != 'needs_review'
```

### Testy integracyjne

**`tests/integration/test_admin_review_endpoints.py`**

```python
# Wymagane przypadki:

# GET /admin/documents/review-queue
# - 200 dla Admin z dokumentami w needs_review
# - 200 dla Owner
# - 403 dla Contributor (brak uprawnienia documents:review)
# - 403 dla Viewer
# - 401 bez tokenu JWT
# - Paginacja: page_size=2, 3 dokumenty → has_next_page=True

# GET /admin/documents/{id}/review
# - 200 z pelnym szczegolami i preview_url
# - 404 dla dokumentu z innego tenanta (test tenant isolation)
# - 409 dla dokumentu w statusie 'completed' (nie needs_review)

# POST /admin/documents/{id}/approve
# - 200 → status zmieniony na 'queued'
# - 200 → wpis w audit_log istnieje
# - 409 jesli dokument juz zatwierdzony (idempotentnosc)
# - 404 dla cross-tenant IDOR attempt

# POST /admin/documents/{id}/reject
# - 200 z powodem
# - 422 jesli reason jest puste (min_length=10)
# - 422 jesli reason przekracza 1000 znakow
# - 404 dla cross-tenant IDOR attempt

# @pytest.mark.tenant_isolation
# - Admin z tenant_A nie moze approve/reject dokumentu z tenant_B → 404
# - Admin z tenant_A nie widzi dokumentow tenant_B w review-queue
```

### Testy bezpieczenstwa (bloker merge)

```python
@pytest.mark.tenant_isolation
async def test_admin_cannot_approve_cross_tenant_document(client_tenant_a, document_tenant_b):
    """Admin z tenanta A probuje zatwierdzic dokument tenanta B. Musi otrzymac 404."""
    response = await client_tenant_a.post(f"/api/v1/admin/documents/{document_tenant_b.id}/approve", json={})
    assert response.status_code == 404

@pytest.mark.tenant_isolation
async def test_review_queue_scoped_to_tenant(client_tenant_a, document_tenant_a, document_tenant_b):
    """Kolejka recenzji zwraca tylko dokumenty z tenanta uzytkownika."""
    response = await client_tenant_a.get("/api/v1/admin/documents/review-queue")
    ids = [item["id"] for item in response.json()["items"]]
    assert str(document_tenant_b.id) not in ids
    assert str(document_tenant_a.id) in ids
```

---

## Definition of Done

- [ ] Wszystkie 5 endpointow zaimplementowane i przetestowane
- [ ] Testy jednostkowe: 100% pokrycie `AdminReviewService` (wszystkie galecie, w tym rollback Redis)
- [ ] Testy integracyjne: happy path + wszystkie kody bledow z sekcji API Contracts
- [ ] `@pytest.mark.tenant_isolation` — 3 testy cross-tenant: approve, reject, review-queue
- [ ] `docs/03-Specyfikacja-API.md` zaktualizowany o nowe endpointy
- [ ] Uprawnienie `documents:review` dodane do bazy (migracja Alembic)
- [ ] Presigned URL TTL nie przekracza 300 sekund (test jednostkowy mockuje MinIO i weryfikuje parametr expires)
- [ ] Audit log zapisywany atomowo z decyzja (test: sprawdz czy wpis istnieje gdy approve/reject zwroci 200)
- [ ] Brak logow zawierajacych tresci dokumentow, powodow odrzucenia ani presigned URLs
- [ ] `python-reviewer` (profil security) zatwierdzil PR
- [ ] `security-auditor` zatwierdzil po sprawdzeniu listy z sekcji Security Checklist
