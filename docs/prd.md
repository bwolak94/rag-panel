# PRD — Uniwersalna Platforma RAG (Retrieval-Augmented Generation)

**Wersja:** 0.1 (draft) · **Data:** 2026-07-09 · **Autor:** Bartosz
**Pilot:** przychodnia medyczna · **Cel docelowy:** produkt white-label dla dowolnej organizacji (firma, szkoła, urząd)

---

## 1. Cel produktu

Samohostowana platforma RAG umożliwiająca organizacjom bezpieczne "rozmawianie" z własną bazą dokumentów. Dokumenty są wgrywane, automatycznie walidowane i kategoryzowane przez LLM, indeksowane do bazy wektorowej, a użytkownicy zadają pytania przez interfejs czatu (Open WebUI) z kontrolą dostępu opartą o role.

**Kluczowe założenie:** system projektowany pod dane wrażliwe (pilot medyczny → RODO), więc całość działa on-premise — żadne dane nie opuszczają infrastruktury klienta. Jednocześnie architektura jest generyczna (multi-tenant), by tę samą instalację/produkt wdrożyć w dowolnej branży.

## 2. Problem

Organizacje mają wiedzę rozproszoną w PDF-ach, procedurach, regulaminach i dokumentacji wewnętrznej. Pracownicy tracą czas na szukanie informacji, a dostęp do dokumentów nie jest kontrolowany granularnie. W branżach regulowanych (medycyna, edukacja) dodatkowo nie można używać publicznych chatbotów chmurowych.

## 3. Persony i role (RBAC)

Role są definiowane per **tenant** (organizacja). Uprawnienia przypisane do ról, role do użytkowników.

| Uprawnienie | Administrator | Pracownik | User |
|---|---|---|---|
| Zarządzanie użytkownikami i rolami | ✅ | ❌ | ❌ |
| Konfiguracja tenanta (kolekcje, modele, limity) | ✅ | ❌ | ❌ |
| Upload dokumentów | ✅ | ✅ | ❌ |
| Zatwierdzanie/odrzucanie dokumentów po walidacji LLM | ✅ | opcjonalnie (flaga) | ❌ |
| Usuwanie dokumentów / reindeksacja | ✅ | własne | ❌ |
| Zadawanie pytań (chat RAG) | ✅ | ✅ | ✅ |
| Dostęp do kolekcji dokumentów | wszystkie | wg przypisania | wg przypisania |
| Wybór modelu LLM | ✅ | wg polityki | wg polityki |
| Wgląd w audit log | ✅ | ❌ | ❌ |
| Historia rozmów | wszystkich (audyt) | własna | własna |

Przykład mapowania w pilocie medycznym: Administrator = kierownik IT przychodni, Pracownik = lekarz/rejestratorka (upload procedur, standardów), User = personel korzystający z wyszukiwania. W szkole: dyrektor / nauczyciel / uczeń.

**Model uprawnień:** role → zbiór permissions (string, np. `documents:upload`, `documents:approve`, `chat:query`, `admin:users`). Dostęp do treści egzekwowany na dwóch poziomach: API (endpoint) i retrieval (filtr metadanych w bazie wektorowej — użytkownik nigdy nie dostanie chunków z kolekcji, do której nie ma dostępu).

## 4. Wymagania funkcjonalne

### FR-1: Uwierzytelnianie i autoryzacja
- Logowanie przez OIDC (Keycloak) — SSO, JWT z rolami i tenant_id w claims.
- RBAC egzekwowany middleware'em w API; każde żądanie zawiera kontekst: user, tenant, role, dozwolone kolekcje.

### FR-2: Pipeline ingestu dokumentów (event-driven)

1. **Upload** — użytkownik (rola z `documents:upload`) wgrywa plik przez API/Open WebUI; plik trafia do **MinIO** (bucket per tenant, prefix per kolekcja).
2. **Event** — MinIO bucket notification publikuje zdarzenie `s3:ObjectCreated` do kolejki (Redis Streams / RabbitMQ).
3. **Walidacja LLM** — worker pobiera dokument, ekstrahuje tekst i uruchamia graf walidacyjny (LangGraph):
   - klasyfikacja typu i kategorii dokumentu (np. procedura / regulamin / cennik / śmieć),
   - ocena jakości i czytelności ekstrakcji (skan bez OCR → odrzucenie lub OCR),
   - detekcja duplikatów (hash + podobieństwo semantyczne),
   - detekcja PII/danych wrażliwych → tagowanie lub wymóg akceptacji admina,
   - wynik: `accepted` / `needs_review` / `rejected` + przypisana kategoria i tagi.
4. **Segregacja** — dokument dostaje metadane (tenant, kolekcja, kategoria, tagi, poziom dostępu, autor, data) zapisane w Postgres.
5. **Chunking i indeksacja** — moduł LangChain: parsowanie (unstructured/docling), podział na chunki (rekurencyjny/semantyczny, konfigurowalny per typ dokumentu), embedding, zapis do **Qdrant** z pełnym payloadem metadanych.
6. **Status** — każdy etap raportuje status do Postgres (`uploaded → validating → indexing → ready / rejected / failed`); UI pokazuje postęp.

Formaty wejściowe (MVP): PDF, DOCX, TXT, MD, HTML. Później: XLSX, PPTX, obrazy z OCR.

### FR-3: Kolekcje wiedzy i onboarding organizacji
- Tenant = organizacja; wewnątrz tenanta **kolekcje** (np. "Procedury medyczne", "HR", "Cenniki").
- Kreator onboardingu: admin tworzy tenanta, definiuje kolekcje, przypisuje role/dostępy, wykonuje masowy import dokumentów (upload folderu / bucket sync).
- Kolekcja ma własną konfigurację: strategia chunkingu, model embeddingów, próg walidacji, widoczność dla ról.

### FR-4: Chat RAG (LangGraph)

Orkiestracja zapytania jako graf (LangGraph):

```
pytanie → [klasyfikacja intencji] → [przepisanie zapytania] → [retrieval z Qdrant
(filtr: tenant + kolekcje dozwolone dla usera)] → [ocena trafności chunków (grader)]
→ (za mało kontekstu? → ponowny retrieval / odpowiedź "nie wiem") → [generacja
odpowiedzi z cytowaniami] → [guardrails wyjścia] → odpowiedź + źródła
```

- Odpowiedzi zawsze z cytowaniami (dokument, strona/sekcja, link do pliku w MinIO).
- Brak wiedzy → jawne "nie znalazłem w dokumentach" (minimalizacja halucynacji).
- Guardrails per branża (pilot medyczny: dopisek, że odpowiedź nie jest poradą medyczną).
- Stan grafu checkpointowany w Postgres (LangGraph checkpointer) — wznawianie i debug.

### FR-5: Wybór modeli
- Warstwa abstrakcji modeli: rejestr modeli per tenant (nazwa, provider, endpoint, limity).
- MVP: modele lokalne przez **Ollama/vLLM** (np. Llama 3.x, Qwen, Bielik dla polskiego); architektura pozwala dopiąć OpenAI-compatible API, jeśli klient wyrazi zgodę.
- Użytkownik wybiera model w Open WebUI (lista filtrowana wg polityki roli); embeddingi ustalane per kolekcja (zmiana = reindeksacja).

### FR-6: Historia i persystencja (PostgreSQL)
- Historia rozmów (konwersacje, wiadomości, użyte źródła, model, tokeny, czas) — własne tabele API + natywne wsparcie Postgres w Open WebUI.
- Metadane dokumentów, statusy pipeline'u, tenanty, użytkownicy, role, audit log, feedback (👍/👎) — wszystko w Postgres.
- Retencja konfigurowalna per tenant (RODO: prawo do usunięcia — kasowanie kaskadowe: Postgres + Qdrant + MinIO).

### FR-7: Frontend — Open WebUI
- Open WebUI jako gotowy frontend czatu: multi-model, historia, RAG UI, multi-user.
- Integracja z backendem przez **OpenAI-compatible API** wystawiane przez nasz serwis RAG (Open WebUI widzi nasz pipeline jako "model") oraz/lub Open WebUI Pipelines.
- SSO przez Keycloak (Open WebUI wspiera OIDC); mapowanie ról.
- Panel administracyjny (zarządzanie dokumentami, walidacja, użytkownicy) — osobny lekki panel (MVP: proste UI, np. wbudowane strony admina) — Open WebUI nie pokryje całego flow dokumentowego.

### FR-8: Audyt i zgodność
- Audit log: kto, co, kiedy (upload, akceptacja, zapytanie, dostęp do dokumentu).
- RODO: rejestr przetwarzania, usuwanie danych na żądanie, dane nie opuszczają infrastruktury.
- Anonimizacja/pseudonimizacja PII w logach.

## 5. Wymagania niefunkcjonalne

| Obszar | Wymaganie |
|---|---|
| Bezpieczeństwo | TLS wszędzie, szyfrowanie at-rest (MinIO SSE, dyski), sekrety w Vault/env, izolacja tenantów na poziomie danych |
| Wydajność | odpowiedź RAG < 10 s (p95, model lokalny), retrieval < 500 ms, ingest dokumentu < 5 min |
| Skalowalność | workery ingestu skalowane horyzontalnie; Qdrant i vLLM na osobnych zasobach (GPU) |
| Dostępność | 99,5% w godzinach pracy; degradacja łagodna (brak LLM → komunikat, nie crash) |
| Obserwowalność | logi strukturalne, metryki (Prometheus/Grafana), tracing pipeline'ów (Langfuse — self-hosted) |
| Deployment | Docker Compose (MVP) → Kubernetes/Helm (skala); pełny on-prem |
| Język | UI i modele: polski + angielski; embeddingi wielojęzyczne |

## 6. Stack technologiczny

| Warstwa | Technologia | Uzasadnienie |
|---|---|---|
| Język | Python 3.12 | ekosystem AI/RAG |
| API backend | FastAPI + Pydantic | async, OpenAPI, walidacja, łatwe wystawienie OpenAI-compatible endpointu |
| Orkiestracja LLM | **LangGraph** (+ LangChain) | grafy z wymaganiami: walidacja ingestu i pipeline zapytań jako stany, checkpointing w Postgres |
| Indeksacja/ingest | LangChain (loaders, splitters) + unstructured/docling | parsowanie wielu formatów, chunking konfigurowalny |
| Baza wektorowa | **Qdrant** | self-hosted, szybkie filtrowanie payloadu (klucz dla RBAC/multi-tenancy), integracja LangChain |
| Object storage | **MinIO** | S3-compatible on-prem, bucket notifications (event-driven ingest) |
| Kolejka zdarzeń | Redis Streams (MVP) → RabbitMQ | odbiór eventów MinIO, kolejka zadań workerów |
| Baza relacyjna | **PostgreSQL 16** | historia rozmów, metadane, RBAC, audit, LangGraph checkpoints |
| Serwowanie LLM | Ollama (MVP) → vLLM (produkcja, GPU) | modele lokalne, OpenAI-compatible API, wymienność modeli |
| Embeddingi | lokalny model ST (np. BGE-M3 — wielojęzyczny) | polski + angielski, on-prem |
| Frontend | **Open WebUI** | gotowy czat multi-model, OIDC, Postgres, integracja przez OpenAI-compatible API |
| AuthN/AuthZ | Keycloak (OIDC) + RBAC w API | SSO, role w JWT, standard enterprise |
| Observability | Prometheus + Grafana + Langfuse (self-hosted) | metryki infra + tracing LLM |
| Deployment | Docker Compose → Kubernetes + Helm | on-prem, powtarzalne wdrożenia per klient |

## 7. Architektura — przepływy

```
                         ┌──────────────┐
        użytkownik ────► │  Open WebUI  │ ◄── OIDC ── Keycloak
                         └──────┬───────┘
                     OpenAI-compatible API
                         ┌──────▼───────┐
                         │  RAG API     │──────────► PostgreSQL
                         │  (FastAPI)   │   historia, RBAC, metadane,
                         └──┬───────┬───┘   audit, checkpoints
                 query      │       │  upload
              ┌─────────────▼┐     ┌▼──────────┐
              │  LangGraph   │     │   MinIO   │
              │ query graph  │     └────┬──────┘
              └──┬───────┬───┘          │ s3:ObjectCreated
                 │       │         ┌────▼──────┐
             retrieval  LLM        │  kolejka  │ (Redis Streams)
              ┌──▼───┐ ┌─▼────┐    └────┬──────┘
              │Qdrant│ │Ollama│    ┌────▼───────────────────────┐
              └──▲───┘ │/vLLM │    │ Ingest worker (LangGraph)  │
                 │     └──▲───┘    │ ekstrakcja → walidacja LLM │
                 │        └────────│ → segregacja → chunking →  │
                 └─── indeksacja ──│ embedding → Qdrant         │
                                   └────────────────────────────┘
```

**Egzekwowanie dostępu przy retrievalu:** każdy chunk w Qdrant ma payload `{tenant_id, collection_id, access_level, category, doc_id}`. Zapytanie zawsze filtruje po `tenant_id` + liście kolekcji z uprawnień użytkownika — izolacja danych jest strukturalna, nie umowna.

## 8. Model danych (skrót)

**Postgres:** `tenants`, `users`, `roles`, `permissions`, `user_roles`, `collections`, `collection_access` (rola↔kolekcja), `documents` (status, kategoria, ścieżka MinIO, hash), `ingestion_jobs`, `conversations`, `messages` (+ użyte źródła), `models_registry`, `audit_log`, `langgraph_checkpoints`.

**Qdrant:** jedna kolekcja fizyczna na model embeddingów; separacja logiczna payloadem (`tenant_id`, `collection_id`) — prostsze operacyjnie niż kolekcja per tenant, z opcją wydzielenia dużych tenantów.

## 9. Zakres MVP i fazy

**Faza 1 (MVP):** 1 tenant (przychodnia), 3 role, upload → MinIO → event → walidacja LLM → Qdrant, chat z cytowaniami przez Open WebUI, historia w Postgres, 2 modele lokalne do wyboru, Docker Compose.

**Faza 2:** multi-tenant, kreator onboardingu, panel admina dokumentów (kolejka `needs_review`), audit log, Langfuse, vLLM na GPU.

**Faza 3:** Kubernetes/Helm, OCR, hybrydowy retrieval (BM25 + wektory + reranker), API dla integracji zewnętrznych, ewaluacja jakości (RAGAS).

## 10. Metryki sukcesu

Trafność odpowiedzi (ocena 👍 ≥ 80%), odsetek odpowiedzi z poprawnymi cytowaniami, czas odpowiedzi p95 < 10 s, odsetek dokumentów poprawnie sklasyfikowanych przez walidator ≥ 90%, adopcja (aktywni użytkownicy tygodniowo).

## 11. Ryzyka i pytania otwarte

- **Jakość polskich modeli lokalnych** — do benchmarku (Bielik, Qwen, Llama PL-finetune); ryzyko: potrzeba GPU o większej pamięci.
- **Open WebUI a RBAC dokumentowy** — Open WebUI pokrywa czat, ale flow walidacji/akceptacji dokumentów wymaga własnego panelu; zakres tego panelu do doprecyzowania.
- **Walidacja LLM** — koszt/czas walidacji dużych dokumentów; strategia: walidacja na próbce + metadanych.
- **Zmiana modelu embeddingów** = pełna reindeksacja — wybór modelu na start jest decyzją długoterminową.
- **RODO** — czy historia rozmów w pilocie medycznym może zawierać dane pacjentów? Rekomendacja: polityka zakazu + detekcja PII w promptach (do decyzji).
- Sprzęt klienta (GPU?) — determinuje wybór rozmiaru modeli.