# Roadmapa i backlog MVP

**Wersja:** 0.1 · Zespół założony: 2–3 dev (Python) · Sprinty 2-tygodniowe

## 1. Roadmapa

| Faza | Czas (szac.) | Zakres | Kryterium wyjścia |
|---|---|---|---|
| 0. Fundamenty | 2 tyg. | repo, CI, Docker Compose (wszystkie usługi), Keycloak + JWT w API, szkielet FastAPI | `docker compose up` stawia całość; login działa |
| 1. MVP | 8 tyg. | pełny ingest pipeline, query graph, Open WebUI, historia, 2 modele | pilot wewnętrzny: 20 dokumentów, 5 użytkowników, 3 role |
| 2. Pilot produkcyjny | 6 tyg. | panel review, audit, Langfuse, hardening, DPIA, backup | wdrożenie w przychodni |
| 3. Produkt | ciągłe | multi-tenant onboarding, K8s/Helm, OCR, hybrydowy retrieval, RAGAS | drugi klient (inna branża) wdrożony z konfiguracji, bez zmian kodu |

## 2. Epiki i user stories (MVP)

### E1 — Autoryzacja i role
- US-1.1: Jako użytkownik loguję się przez SSO i widzę tylko funkcje mojej roli. *(AC: JWT z rolami; 403 dla brakujących uprawnień; testy per rola)*
- US-1.2: Jako admin przypisuję role i dostęp do kolekcji. *(AC: zmiana działa bez re-loginu ≤ 60 s — cache)*

### E2 — Ingest dokumentów
- US-2.1: Jako pracownik wgrywam PDF do kolekcji i widzę status przetwarzania. *(AC: statusy na żywo; błąd = czytelny komunikat)*
- US-2.2: Jako system waliduję dokument LLM-em i nadaję kategorię. *(AC: ≥90% poprawnych kategorii na zbiorze testowym 50 dok.)*
- US-2.3: Jako system odrzucam duplikaty i śmieciowe pliki. *(AC: ten sam plik 2× → rejected_duplicate)*
- US-2.4: Jako admin widzę kolejkę `needs_review` i akceptuję/odrzucam. *(AC: akceptacja wznawia indeksację)*
- US-2.5: Jako system indeksuję dokument do Qdrant z metadanymi. *(AC: chunki mają pełny payload; dokument wyszukiwalny ≤ 5 min od uploadu)*

### E3 — Chat RAG
- US-3.1: Jako użytkownik zadaję pytanie i dostaję odpowiedź z cytowaniami. *(AC: każda odpowiedź merytoryczna ma ≥1 źródło z linkiem)*
- US-3.2: Jako użytkownik dostaję "nie znalazłem", gdy wiedzy brak. *(AC: pytanie spoza korpusu nie generuje halucynacji — zbiór testowy 20 pytań-pułapek)*
- US-3.3: Jako użytkownik wybieram model/pipeline z listy. *(AC: lista filtrowana rolą)*
- US-3.4: Jako użytkownik widzę historię rozmów i mogę ją usunąć. *(AC: usunięcie znika też z Postgres API)*
- US-3.5: Jako użytkownik nie mam dostępu do wiedzy z cudzych kolekcji. *(AC: test izolacji w CI — blocker release'u)*

### E4 — Administracja
- US-4.1: Jako admin tworzę kolekcje i konfiguruję chunking/widoczność.
- US-4.2: Jako admin zarządzam rejestrem modeli.
- US-4.3: Jako admin przeglądam audit log. *(faza 2)*

## 3. Definicja ukończenia (DoD)

Kod z review, testy jednostkowe + integracyjne (testcontainers: Postgres, Qdrant, MinIO, Redis), test izolacji tenantów zielony, lint/typing (ruff, mypy), dokumentacja endpointu w OpenAPI, metryki i logi dla nowej ścieżki.

## 4. Plan testów (skrót)

| Poziom | Zakres | Narzędzia |
|---|---|---|
| Jednostkowe | logika grafów (węzły izolowane), RBAC, chunking | pytest |
| Integracyjne | pipeline ingest end-to-end na testcontainers, kontrakty API | pytest + testcontainers |
| Jakość RAG | zestaw ewaluacyjny: 50 par pytanie–oczekiwane źródło + 20 pytań-pułapek; metryki: trafność cytowań, faithfulness | RAGAS / własny harness (faza 2) |
| Bezpieczeństwo | izolacja tenantów, IDOR, autoryzacja per endpoint | pytest + pen-test przed produkcją |
| Wydajność | 20 równoczesnych zapytań chat, ingest 100 dokumentów | locust |

## 5. Zależności i ryzyka harmonogramu

- **GPU u klienta** — zamówić/potwierdzić przed fazą 2 (najdłuższy lead time).
- **Benchmark modeli PL** (Bielik vs Qwen vs Llama) — sprint 1 fazy 1, bo wybór wpływa na prompty i jakość walidatora.
- **Decyzja o danych pacjentów w historii czatu** — wymagana od IOD przychodni przed fazą 2 (blokuje DPIA).
- **Wybór modelu embeddingów** — zamrozić do końca fazy 1 (zmiana = reindeksacja).

## 6. Otwarte decyzje do podjęcia

1. Panel admina: FastAPI+HTMX (szybciej) czy React (ładniej, wolniej)?
2. Czy Open WebUI w trybie multi-tenant, czy instancja per tenant? (rekomendacja MVP: instancja per tenant — prostsza izolacja)
3. Licencjonowanie produktu przy wdrożeniach u klientów (per tenant / per user / flat).