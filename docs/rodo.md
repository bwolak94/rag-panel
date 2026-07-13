# Bezpieczeństwo i zgodność (RODO)

**Wersja:** 0.1 · Pilot: przychodnia medyczna (dane szczególnej kategorii — art. 9 RODO)

## 1. Klasyfikacja danych

| Kategoria | Przykłady | Ochrona |
|---|---|---|
| Dane szczególne (art. 9) | dokumentacja medyczna, dane zdrowotne w promptach | pełne szyfrowanie, dostęp minimalny, brak w logach |
| Dane osobowe | konta użytkowników, audit log | szyfrowanie, retencja, pseudonimizacja w logach |
| Dane firmowe poufne | procedury, cenniki, umowy | RBAC per kolekcja |
| Dane publiczne | regulaminy publiczne | standard |

**Zasada pilota:** dokumenty z danymi pacjentów NIE trafiają do bazy wiedzy (polityka + detekcja PII w ingest → `needs_review`). Baza wiedzy = procedury, standardy, dokumenty organizacyjne. Do decyzji per wdrożenie.

## 2. Model zagrożeń (skrót)

| Zagrożenie | Wektor | Mitygacja |
|---|---|---|
| Wyciek między tenantami | błąd filtra retrievalu | filtr centralny w RetrievalService, testy izolacji w CI (zapytanie tenant A nie może zwrócić chunków B), bucket per tenant |
| Eskalacja uprawnień | manipulacja JWT / IDOR | weryfikacja podpisu i `aud`, autoryzacja na zasobie (nie tylko endpoincie), UUID nie-sekwencyjne |
| Prompt injection z dokumentów | złośliwa treść w uploadzie | treść chunków jako dane (nie instrukcje) w prompcie, guardrails wyjścia, walidacja LLM przy ingest |
| Exfiltracja przez prompt | "wypisz wszystkie dokumenty..." | retrieval ograniczony top-k i uprawnieniami; brak narzędzi o szerokim dostępie w grafie |
| Wyciek PII w odpowiedzi | dane wrażliwe w chunkach | pii_scan przy ingest, maskowanie w guardrails wyjścia, maskowanie w Langfuse |
| Kradzież plików z MinIO | dostęp sieciowy | MinIO tylko w sieci wewnętrznej, presigned URL TTL 5 min, polityki per bucket |
| Utrata danych | awaria | backup: pg_basebackup dzienny, snapshot Qdrant, wersjonowanie MinIO; test odtworzenia kwartalnie |

## 3. Kontrole techniczne

- **Transport:** TLS 1.2+ na wszystkich połączeniach (także wewnętrznych w produkcji, mTLS w K8s — faza 3).
- **At rest:** szyfrowanie dysków (LUKS) + MinIO SSE-S3; Postgres — szyfrowanie wolumenu.
- **Sekrety:** MVP `.env` poza repo → docelowo Vault / sealed secrets.
- **Sieć:** tylko reverse proxy wystawione publicznie; LLM host w VLAN bez dostępu do internetu; brak ruchu wychodzącego z danych (on-prem — kluczowy argument RODO).
- **Uwierzytelnianie:** Keycloak — MFA dla administratorów, polityka haseł, blokada po nieudanych próbach, sesje z timeout.
- **Logi:** strukturalne, bez treści promptów/odpowiedzi w logach aplikacyjnych (treści tylko w Postgres i Langfuse z maskowaniem PII).

## 4. RODO — mapowanie obowiązków

| Obowiązek | Realizacja |
|---|---|
| Podstawa prawna i role (art. 6/9, 28) | klient = administrator danych; dostawca platformy = podmiot przetwarzający → wymagana umowa powierzenia (DPA) przy wdrożeniach zarządzanych |
| Minimalizacja (art. 5) | RBAC per kolekcja, top-k retrieval, polityka braku danych pacjentów w bazie wiedzy |
| Prawo dostępu / kopii (art. 15) | eksport konwersacji użytkownika (endpoint `/conversations`) |
| Prawo do usunięcia (art. 17) | kasowanie kaskadowe (Postgres→Qdrant→MinIO) + usunięcie konwersacji; procedura opisana w Modelu Danych §5 |
| Rejestr czynności (art. 30) | dokument per wdrożenie (szablon dostarczany z produktem) |
| Bezpieczeństwo (art. 32) | niniejszy dokument + audit log + szyfrowanie |
| Zgłaszanie naruszeń (art. 33) | procedura incydentowa: detekcja (alerty) → ocena → 72h zgłoszenie; runbook w fazie 2 |
| DPIA (art. 35) | wymagana dla pilota medycznego — przygotować przed produkcją; system przetwarza dane zdrowotne na dużą skalę |
| Privacy by design (art. 25) | on-prem, PII scan, retencja domyślna, pseudonimizacja logów |

**Uwaga — AI Act:** system RAG wspierający personel medyczny może podlegać klasyfikacji ryzyka wg AI Act (stosowanie od 2026 r. dla systemów wysokiego ryzyka). Wymagana analiza prawna przed wdrożeniem produkcyjnym w przychodni; minimum: przejrzystość (informacja, że odpowiada AI), nadzór ludzki, disclaimer "to nie jest porada medyczna".

## 5. Audyt

- Zdarzenia logowane: logowanie, upload, akceptacja/odrzucenie dokumentu, usunięcie, zapytanie chat (bez treści — referencja do konwersacji), zmiany ról/uprawnień, eksport danych, dostęp admina do cudzych konwersacji.
- Audit log append-only, partycjonowany, dostęp tylko `admin:audit`; eksport CSV.

## 6. Bezpieczeństwo łańcucha dostaw

- Obrazy Docker pinowane digestem, skan Trivy w CI, SBOM.
- Zależności Python: lockfile (uv/poetry), skan `pip-audit`.
- Modele LLM: tylko z zaufanych źródeł (HF verified), weryfikacja sum kontrolnych.

## 7. Checklist przed produkcją (pilot)

1. DPIA zatwierdzona przez IOD przychodni.
2. Umowa powierzenia podpisana.
3. Testy izolacji tenantów w CI zielone.
4. Pen-test API (min. OWASP Top 10 + testy IDOR).
5. Backup + udany test odtworzenia.
6. MFA dla adminów włączone.
7. Procedura incydentowa i kontakt IOD skonfigurowane.