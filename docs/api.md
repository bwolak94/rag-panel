# Specyfikacja API — RAG API (FastAPI)

**Wersja:** 0.1 · **Baza:** `https://<host>/api/v1` · **Auth:** Bearer JWT (Keycloak OIDC)

## 1. Zasady ogólne

- Każde żądanie: `Authorization: Bearer <JWT>`; z tokena wyciągane `sub`, `tenant_id`, `roles`.
- Autoryzacja: wymagane uprawnienie podane przy każdym endpoincie; brak → `403`.
- Paginacja: `?page`, `?page_size` (max 100); odpowiedzi listowe: `{items, total, page}`.
- Błędy: `{error: {code, message, details}}`; kody: `401` brak/zły token, `403` brak uprawnienia, `404`, `409` konflikt (np. duplikat), `422` walidacja, `429` rate limit.
- Wersjonowanie w ścieżce (`/v1`); zmiany łamiące → `/v2`.

## 2. Endpointy OpenAI-compatible (dla Open WebUI)

| Metoda | Ścieżka | Uprawnienie | Opis |
|---|---|---|---|
| GET | `/v1/models` | `chat:query` | lista "modeli": pipeline'y RAG (per kolekcja/tryb) + surowe LLM wg polityki roli |
| POST | `/v1/chat/completions` | `chat:query` | czat; `stream: true` → SSE; model = id pipeline'u RAG |

Rozszerzenia w odpowiedzi completions: pole `message_sources` (lista `MessageSourceOut`) dołączane w ostatnim chunku SSE i w odpowiedzi non-streaming.

### Schema `MessageSourceOut`

```json
{
  "document_id": "uuid-string",
  "collection_id": "uuid-string",
  "document_title": "string",
  "section_heading": "string | null",
  "source_url": "string | null",
  "chunk_id": "uuid-string | null",
  "page_number": "int | null",
  "highlight_text": "string | null",
  "relevance_score": "float"
}
```

### Query graph (TASK-010)

`POST /v1/chat/completions` uruchamia LangGraph query graph:
`classify_intent → rewrite_query → retrieve → grade_documents → generate → guardrails_output`

- `intent == "topical"` → pełny retrieval; `chitchat`/`out_of_scope` → odpowiedź guardrails (po polsku).
- `no_results == true` → odpowiedź "nie znalazłem" (bez halucynacji).
- `guardrails.add_disclaimer == true` → disclaimer medyczny dołączany do odpowiedzi.
- Tokeny (prompt/completion) z odpowiedzi LLM persystowane w tabeli `messages`.

## 3. Dokumenty i ingest

| Metoda | Ścieżka | Uprawnienie | Opis |
|---|---|---|---|
| POST | `/documents/upload` | `documents:upload` | multipart: plik + `collection_id` (+ opcjonalne tagi); zwraca `document_id`, `job_id`; API zapisuje do MinIO (event startuje pipeline) |
| GET | `/documents` | `documents:read` | lista z filtrem: `collection_id`, `status`, `category`, `q` |
| GET | `/documents/{id}` | `documents:read` | metadane + status ingestu + wynik walidacji LLM |
| GET | `/documents/{id}/download` | `documents:read` | presigned URL MinIO (TTL 5 min) |
| DELETE | `/documents/{id}` | `documents:delete` (własne: `documents:delete_own`) | kasowanie kaskadowe: MinIO + chunki Qdrant + metadane; wpis audit |
| POST | `/documents/{id}/reindex` | `documents:manage` | ponowny chunking/embedding (np. po zmianie konfiguracji) |
| GET | `/documents/review-queue` | `documents:approve` | dokumenty `needs_review` (wynik walidacji, powód) |
| POST | `/documents/{id}/review` | `documents:approve` | body: `{decision: approve/reject, note}`; approve → wznowienie grafu ingestu |
| GET | `/ingestion-jobs/{job_id}` | `documents:read` | status pipeline'u per etap |

## 4. Kolekcje

| Metoda | Ścieżka | Uprawnienie | Opis |
|---|---|---|---|
| GET | `/collections` | `chat:query` | kolekcje widoczne dla usera |
| POST | `/collections` | `admin:collections` | nazwa, opis, konfiguracja chunkingu, model embeddingów, widoczność ról |
| PATCH | `/collections/{id}` | `admin:collections` | zmiana konfiguracji (zmiana embeddingów → wymusza reindeksację, `409` bez `?force`) |
| DELETE | `/collections/{id}` | `admin:collections` | kaskadowo z dokumentami |
| PUT | `/collections/{id}/access` | `admin:collections` | mapa rola → poziom dostępu |

## 5. Administracja (tenant)

| Metoda | Ścieżka | Uprawnienie | Opis |
|---|---|---|---|
| GET/POST/PATCH/DELETE | `/users` | `admin:users` | CRUD użytkowników (provisioning przez Keycloak Admin API; tu przypisania ról/kolekcji) |
| GET/POST/PATCH | `/roles` | `admin:users` | role i ich permissions |
| GET/POST/PATCH/DELETE | `/models` | `admin:models` | rejestr modeli: nazwa, endpoint, typ (llm/embedding), dozwolone role, limity |
| GET | `/audit-log` | `admin:audit` | filtr: user, akcja, zakres dat |
| GET | `/usage` | `admin:audit` | statystyki: zapytania, tokeny, dokumenty per okres |

## 6. Konwersacje

| Metoda | Ścieżka | Uprawnienie | Opis |
|---|---|---|---|
| GET | `/conversations` | `chat:query` | własne; admin z `admin:audit` — wszystkie (tryb audytu) |
| GET | `/conversations/{id}` | j.w. | wiadomości + użyte źródła |
| DELETE | `/conversations/{id}` | `chat:query` | usunięcie własnej (RODO) |
| POST | `/messages/{id}/feedback` | `chat:query` | `{rating: up/down, comment}` |

## 7. Systemowe

| Metoda | Ścieżka | Opis |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/readyz` | zależności: Postgres, Qdrant, MinIO, Redis, LLM endpoint |
| GET | `/metrics` | Prometheus |

## 8. Przepływ autoryzacji retrievalu (krytyczne)

1. Middleware dekoduje JWT → `user_ctx = {user_id, tenant_id, roles, permissions}`.
2. Dependency `allowed_collections(user_ctx)` odczytuje `collection_access` (cache 60 s).
3. Każde zapytanie do Qdrant budowane wyłącznie przez `RetrievalService`, który **zawsze** dokleja filtr `tenant_id == ctx.tenant AND collection_id IN allowed`. Żaden inny moduł nie ma bezpośredniego klienta Qdrant (egzekwowane review + test architektury).

## 9. Rate limiting i limity

- Per user: 30 zapytań chat/min; per tenant: konfigurowalne (rejestr w Postgres, licznik w Redis).
- Upload: max 100 MB/plik (MVP), max 20 równoległych jobów ingestu per tenant.