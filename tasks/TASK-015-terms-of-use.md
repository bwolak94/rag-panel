# TASK-015: Warunki korzystania z systemu — tresc, egzekwowanie, onboarding najemcy

**Status:** TODO
**Priorytet:** P1 — wymagane przed udostepnieniem systemu jakiemukolwiek tenantowi produkcyjnemu
**Wlasciciel:** backend-dev + design
**Recenzent:** security-auditor + python-reviewer
**Powiazane dokumenty:** `docs/rodo.md`, `docs/architecture.md`, `docs/data-model.md`, `docs/03-Specyfikacja-API.md`
**Szacowany naklad:** 4–6 dni

---

## Overview

Zadanie obejmuje trzy powiazane ze soba obszary:

1. **Tresc Warunkow korzystania (WK)** — dokument prawno-informacyjny, ktory kazdy tenant (reprezentowany przez Wlasciciela) musi zaakceptowac przed uzyciem platformy. Tresc jest dopasowana do kontekstu medycznego (pilot: przychodnia), polskiego prawa i RODO.

2. **Mechanizm egzekwowania** — API i middleware blokujace dostep do calego systemu dla tenantow, ktorzy nie zaakceptowali aktualnej wersji WK. Nowa wersja WK wymaga ponownej akceptacji przez wszystkich tenantow.

3. **Onboarding flow** — sekwencja krokow przy tworzeniu nowego tenanta: rejestracja → wyswietlenie WK → akceptacja → pierwsze logowanie do systemu.

Kontekst: dane medyczne to dane szczegolnej kategorii (RODO Art. 9). Platforma musi miec zdefiniowany podstawy prawny przetwarzania i musi go zakomunikowac uzytkownikowi w sposob zrozumialy, zanim jakiekolwiek przetwarzanie nastapi.

---

## Usage

**Przeplywy:**

| Aktor | Scenariusz |
|---|---|
| Wlasciciel nowego tenanta | Rejestruje tenant → widzi WK → klika "Akceptuje" → uzyskuje dostep |
| Wlasciciel istniejacego tenanta | Nowa wersja WK → przy kolejnym logowaniu widzi ekran "WK zostaly zaktualizowane" → musi zaakceptowac przed kontynuacja |
| Admin tenanta | Moze sprawdzic status akceptacji WK przez Wlasciciela; nie moze sam zaakceptowac (tylko Wlasciciel) |
| Uzytkownik (Viewer/Contributor) | Nie widzi WK; dostep blokowany przez middleware jesli Wlasciciel nie zaakceptowal; komunikat: "Administrator Twojej organizacji musi zaakceptowac warunki korzystania" |
| Integracja API (klucz API) | Klucze API omijaja sprawdzanie WK (systemy automatyczne); wymagana dokumentacja tego wyjatku |

---

## Tech Stack

- **Framework:** FastAPI, middleware (Starlette `BaseHTTPMiddleware`)
- **Baza danych:** PostgreSQL 16, SQLAlchemy async ORM, Alembic
- **Cache:** Redis (TTL 60 s) dla statusu akceptacji WK per tenant — unikamy zapytania DB przy kazdym zadaniu
- **Autoryzacja:** JWT (Keycloak); rola Owner wymagana do akceptacji
- **Tresc WK:** przechowywana w tabeli `tos_versions` — wersjonowanie, nie pliki statyczne
- **IP klienta:** pobrany z `request.client.host` lub `X-Forwarded-For` (za reverse proxy); zapisany przy akceptacji
- **User-Agent:** z naglowka HTTP; zapisany przy akceptacji (audit trail)

---

## Database Patterns

### Nowe tabele — uzasadnienie wyboru

**Decyzja projektowa:** Uzyc dedykowanych tabel `tos_versions` i `tos_acceptances` zamiast `tenants.settings JSONB`.

**Uzasadnienie:**
- `settings JSONB` nie nadaje sie do przechowywania wielowierszowej tresci (TEXT), historii wersji ani wielokrotnych rekordow akceptacji.
- Dedykowane tabele pozwalaja na pelny audit trail: kto zaakceptowal, kiedy, z jakiego IP, dla ktorej wersji.
- Tabela `tos_versions` pozwala na maszyne stanow wersji (draft → active → superseded) bez migracji schematu.
- Zapytanie "czy aktualny WK jest zaakceptowany dla tenanta X" jest proste i indeksowalnie efektywne.

### DDL nowych tabel

```sql
-- Wersje warunkow korzystania (globalne, nie per-tenant)
CREATE TABLE tos_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version         VARCHAR(20) NOT NULL UNIQUE,  -- np. "1.0", "1.1", "2.0"
    content         TEXT NOT NULL,                 -- pelna tresc WK (Markdown lub plain text)
    summary         TEXT NOT NULL,                 -- krotkie podsumowanie zmian (dla UI)
    status          VARCHAR(20) NOT NULL DEFAULT 'draft',
                    -- 'draft' | 'active' | 'superseded'
                    -- Constraint: dokladnie jedna wersja moze byc 'active' jednoczesnie
    effective_date  TIMESTAMPTZ NOT NULL,          -- od kiedy obowiazuje
    created_by      UUID NOT NULL,                 -- FK do users.id (administrator systemu)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Constraint: max jedna aktywna wersja
CREATE UNIQUE INDEX tos_versions_single_active
    ON tos_versions (status)
    WHERE status = 'active';

-- Akceptacje WK przez tenantow
CREATE TABLE tos_acceptances (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),  -- Wlasciciel tenanta
    tos_version_id  UUID NOT NULL REFERENCES tos_versions(id),
    accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_address      INET NOT NULL,
    user_agent      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX tos_acceptances_tenant_version
    ON tos_acceptances (tenant_id, tos_version_id);

CREATE INDEX tos_acceptances_tenant_id
    ON tos_acceptances (tenant_id);
```

**Uwaga dla data-engineer:** Tabela `tos_acceptances` nie ma `tenant_id` w roli "business tenant isolation" — tu `tenant_id` to FK do tenanta, ktorego akceptacja dotyczy. Nie ma sensu filtrowanie po `ctx.tenant_id` tak jak w tabelach domenowych; zamiast tego sprawdzamy: `WHERE tenant_id = :tenant_id AND tos_version_id = :active_version_id`.

### Wzorzec zapytania: sprawdzenie statusu akceptacji

```sql
-- Czy tenant zaakceptowal aktualnie obowiazujace WK?
SELECT EXISTS (
    SELECT 1
    FROM tos_acceptances ta
    JOIN tos_versions tv ON tv.id = ta.tos_version_id
    WHERE ta.tenant_id = :tenant_id
      AND tv.status = 'active'
) AS has_accepted_current_tos;
```

To zapytanie jest wykonywane przy kazdym zadaniu (przez middleware). Wynik jest cache'owany w Redis pod kluczem `tos:tenant:{tenant_id}:accepted` z TTL 60 sekund. Invalidacja cache: po kazdej akceptacji i po aktywacji nowej wersji WK.

---

## API Contracts

Wszystkie endpointy pod prefiksem `/api/v1`. Schematy zgodne z reszta API (RFC 7807 dla bledow).

### Schematy Pydantic

```python
# src/api/schemas/tos.py

from __future__ import annotations
from uuid import UUID
from datetime import datetime
from pydantic import BaseModel, Field


class TosVersionPublic(BaseModel):
    """Reprezentacja publiczna wersji WK — eksponowana przez GET /terms."""
    id: UUID
    version: str
    content: str = Field(..., description="Pelna tresc WK w formacie Markdown")
    summary: str = Field(..., description="Krotkie podsumowanie zmian wzgledem poprzedniej wersji")
    effective_date: datetime
    status: str  # 'active' | 'superseded' (draft nie jest publiczny)


class TosAcceptRequest(BaseModel):
    """Cialo zadania POST /tenants/{id}/terms/accept."""
    tos_version_id: UUID = Field(
        ...,
        description="ID wersji WK ktora jest akceptowana. "
                    "Musi byc zgodne z aktualnie aktywna wersja (z GET /terms)."
    )
    # Oswiadczenie jawnej zgody — frontend musi je wyswietlic i uzytkownik musi zaznaczyc
    explicit_consent: bool = Field(
        ...,
        description="Musi byc True. Reprezentuje swiadome klikniecie checkbox przez Wlasciciela."
    )


class TosAcceptanceRecord(BaseModel):
    """Rekord akceptacji — zwracany po pomyslnej akceptacji i w status response."""
    id: UUID
    tenant_id: UUID
    accepted_by_user_id: UUID
    accepted_by_display_name: str
    tos_version_id: UUID
    tos_version: str
    accepted_at: datetime
    ip_address: str  # zamaskowany: "192.168.1.xxx" — nie ujawniamy pelnego IP w API


class TosStatusResponse(BaseModel):
    """Status akceptacji WK dla danego tenanta."""
    tenant_id: UUID
    current_tos_version: str
    is_accepted: bool
    accepted_at: datetime | None = None
    accepted_by: str | None = None  # display_name Wlasciciela
    requires_reacceptance: bool = Field(
        ...,
        description="True jesli istnieje nowsza aktywna wersja WK niz ostatnio zaakceptowana"
    )
    # Komunikat gotowy do wyswietlenia w UI (po polsku)
    ui_message: str | None = None
```

---

### Endpoint 1: GET /terms

**Opis:** Publiczny endpoint zwracajacy aktualnie obowiazujaca wersje WK. Nie wymaga autoryzacji.

**Uprawnienie:** Brak (publiczny).

**Cache:** Odpowiedz cache'owana przez 5 minut (tresc zmienia sie rzadko).

**Przykladowa odpowiedz 200:**

```json
{
  "id": "t1t2t3t4-...",
  "version": "1.1",
  "content": "# Warunki korzystania z platformy RAG\n\n**Wersja 1.1, obowiazuje od 1 lipca 2026**\n\n## 1. Definicje\n...",
  "summary": "Wersja 1.1 dodaje sekcje dotyczace retencji danych konwersacji (24 miesiac).",
  "effective_date": "2026-07-01T00:00:00Z",
  "status": "active"
}
```

**Odpowiedzi bledow:**

| Kod | Warunek | Tresc |
|---|---|---|
| 404 | Brak aktywnej wersji WK (blad konfiguracji systemu) | `{"title": "Brak warunkow korzystania", "status": 404, "detail": "System nie ma aktywnej wersji warunkow korzystania. Skontaktuj sie z administratorem systemu."}` |

---

### Endpoint 2: POST /tenants/{tenant_id}/terms/accept

**Opis:** Rejestruje akceptacje aktualnych WK przez Wlasciciela tenanta.

**Uprawnienie:** Rola Owner w danym tenacie (`require_role("Owner", tenant_id=tenant_id)`).

**Cialo zadania:**

```json
{
  "tos_version_id": "t1t2t3t4-...",
  "explicit_consent": true
}
```

**Walidacja:**

- `explicit_consent` musi byc `true` — jesli `false`, zwroc 422 z komunikatem "Akceptacja warunkow korzystania jest wymagana do uzycia systemu."
- `tos_version_id` musi odpowiadac aktualnie aktywnej wersji — jesli nie, zwroc 422 z komunikatem "Wersja warunkow korzystania jest nieaktualna. Odswierz strone i zaakceptuj aktualna wersje."
- Idempotentnosc: jesli Wlasciciel juz zaakceptowal te wersje, zwroc 200 z istniejacym rekordem (nie tworzym duplikatu; `INSERT ... ON CONFLICT DO NOTHING` lub SELECT-first).

**Przykladowa odpowiedz 200:**

```json
{
  "id": "a1a2a3a4-...",
  "tenant_id": "ten1ten1-...",
  "accepted_by_user_id": "u1u2u3u4-...",
  "accepted_by_display_name": "Piotr Wisniewsk",
  "tos_version_id": "t1t2t3t4-...",
  "tos_version": "1.1",
  "accepted_at": "2026-07-13T12:00:00Z",
  "ip_address": "192.168.1.xxx"
}
```

**Logika backendowa:**

```
1. Weryfikuj JWT i role Owner dla tenant_id (403 jesli nie Owner)
2. Pobierz aktywna wersje WK; porownaj z tos_version_id z body (422 jesli niezgodne)
3. Sprawdz czy akceptacja juz istnieje (SELECT); jesli tak, zwroc istniejacy rekord
4. W transakcji:
   a. INSERT tos_acceptances (tenant_id, user_id, tos_version_id, ip_address, user_agent)
   b. INSERT audit_log (action='tos.accepted', resource_type='tos_version', resource_id=tos_version_id,
      details={"version": "1.1", "ip": "...", "explicit_consent": true})
5. Invaliduj cache Redis: DEL tos:tenant:{tenant_id}:accepted
6. Zwroc TosAcceptanceRecord
```

**Odpowiedzi bledow:**

| Kod | Warunek | Tresc |
|---|---|---|
| 403 | Uzytkownik nie jest Wlascicielem tenanta | `{"title": "Brak uprawnien", "status": 403, "detail": "Tylko Wlasciciel organizacji moze zaakceptowac warunki korzystania. Skontaktuj sie z administratorem swojej organizacji."}` |
| 422 | `explicit_consent` = false | `{"title": "Wymagana akceptacja", "status": 422, "detail": "Akceptacja warunkow korzystania jest wymagana do uzycia systemu. Zaznacz checkbox, aby potwierdzic akceptacje."}` |
| 422 | Niezgodna wersja WK | `{"title": "Nieaktualna wersja", "status": 422, "detail": "Wersja warunkow korzystania jest nieaktualna. Odswierz strone i zaakceptuj aktualna wersje (1.1)."}` |

---

### Endpoint 3: GET /tenants/{tenant_id}/terms/status

**Opis:** Sprawdza status akceptacji WK dla tenanta. Uzywany przez UI do decyzji czy wyswietlic ekran akceptacji.

**Uprawnienie:** Rola Owner lub Admin w danym tenacie.

**Przykladowe odpowiedzi:**

```json
// Akceptacja aktualna
{
  "tenant_id": "ten1ten1-...",
  "current_tos_version": "1.1",
  "is_accepted": true,
  "accepted_at": "2026-07-01T09:00:00Z",
  "accepted_by": "Piotr Wisniewski",
  "requires_reacceptance": false,
  "ui_message": null
}

// Wymagana ponowna akceptacja (nowa wersja WK)
{
  "tenant_id": "ten1ten1-...",
  "current_tos_version": "1.1",
  "is_accepted": false,
  "accepted_at": null,
  "accepted_by": null,
  "requires_reacceptance": true,
  "ui_message": "Warunki korzystania z systemu zostaly zaktualizowane (wersja 1.1, obowiazuje od 1 lipca 2026). Wlasciciel organizacji musi zaakceptowac nowe warunki przed kontynuacja pracy."
}

// Brak jakiejkolwiek akceptacji (nowy tenant)
{
  "tenant_id": "ten1ten1-...",
  "current_tos_version": "1.1",
  "is_accepted": false,
  "accepted_at": null,
  "accepted_by": null,
  "requires_reacceptance": false,
  "ui_message": "Aby korzystac z systemu, Wlasciciel organizacji musi zaakceptowac warunki korzystania."
}
```

---

### Middleware ToS Check

**Klasa:** `TosCheckMiddleware` w `src/api/middleware/tos_check.py`

**Zachowanie:**
- Przechwytuje wszystkie zadania do `/api/v1/` z wyjatkiem:
  - `GET /api/v1/terms` (publiczny)
  - `POST /api/v1/tenants/{id}/terms/accept`
  - `GET /api/v1/tenants/{id}/terms/status`
  - Wszystkie endpointy `/api/v1/auth/` (login, refresh)
  - Zadania z naglowkiem `X-Api-Key` (klucze API omijaja WK; patrz sekcja "Terms of Use")
- Dla zadania z tokenem JWT: ekstrahuje `tenant_id` z kontekstu JWT, sprawdza cache Redis.
- Jesli `accepted == False`: zwraca 403 z cielem JSON zawierajacym `tos_required: true`.

**Odpowiedz 403 blokujaca dostep:**

```json
{
  "type": "about:blank",
  "title": "Wymagana akceptacja warunkow korzystania",
  "status": 403,
  "detail": "Wlasciciel Twojej organizacji musi zaakceptowac warunki korzystania (wersja 1.1) przed kontynuacja pracy w systemie.",
  "tos_required": true,
  "current_tos_version": "1.1",
  "accept_url": "/api/v1/tenants/{tenant_id}/terms/accept"
}
```

Frontend wykrywa `tos_required: true` i przekierowuje na strone akceptacji WK zamiast wyswietlic generyczny blad.

**Implementacja (sygnatury dla backend-dev):**

```python
# src/api/middleware/tos_check.py

class TosCheckMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, redis: Redis, tos_service: TosService) -> None: ...

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 1. Jesli sciezka na liscie wykluczonych: call_next(request)
        # 2. Jesli naglowek X-Api-Key obecny: call_next(request)
        # 3. Ekstrahuj tenant_id z JWT (nie weryfikuj tutaj — auth middleware zrobi to pozniej)
        #    Jesli brak JWT: call_next (auth middleware zwroci 401)
        # 4. Sprawdz Redis cache: tos:tenant:{tenant_id}:accepted
        # 5. Cache miss: zapytaj DB, zapisz do cache (TTL 60s)
        # 6. Jesli not accepted: zwroc 403 JSON jak powyzej
        # 7. Jesli accepted: call_next(request)
        ...
```

---

## Tresc Warunkow Korzystania

Ponizej tresc WK wersji 1.0 (punkt startowy). Administrator systemu wgrywa ja do tabeli `tos_versions` przy deploymencie (migracja seedujaca). Przyszle zmiany sa wgrane przez dedykowany endpoint (poza MVP: POST /admin/tos-versions, tylko superadmin systemu).

```markdown
# Warunki korzystania z platformy RAG

**Wersja 1.0**
**Data wejscia w zycie: 1 lipca 2026**
**Dostawca:** [Nazwa dostawcy systemu]
**Kontakt:** [adres e-mail dostawcy]

---

## 1. Definicje

**Platforma** oznacza system informatyczny oparty na technologii RAG (Retrieval-Augmented Generation),
udostepniany przez Dostawce w trybie SaaS lub self-hosted.

**Tenant** oznacza organizacje (np. przychodnie, szpital, przedsiebiorstwo) korzystajaca z Platformy
na podstawie odrebnej umowy lub rejestracji.

**Uzytkownik** oznacza osobe fizyczna korzystajaca z Platformy w imieniu Tenanta.

**Dokumenty** oznaczaja pliki przesylane przez Uzytkownikow w celu indeksowania i wyszukiwania.

**Odpowiedzi AI** oznaczaja wyniki generowane przez modele jezykowe na podstawie Dokumentow.

---

## 2. Dozwolone sposoby korzystania

2.1. Platforma jest przeznaczona wylacznie do:
- wyszukiwania informacji w dokumentach organizacji,
- wspomaganego przez AI przeszukiwania baz wiedzy,
- uzyskiwania odpowiedzi na pytania na podstawie wgraznych dokumentow.

2.2. Odpowiedzi AI maja charakter informacyjny. Nie stanowia porady medycznej, prawnej ani
zadnej innej porady profesjonalnej. Uzytkownik jest zobowiazany do weryfikacji odpowiedzi
AI z odpowiednim specjalista przed podjaciem decyzji.

2.3. W kontekscie medycznym: odpowiedzi Platformy NIE zastepuja diagnozy lekarskiej, decyzji
klinicznej ani konsultacji z lekarzem lub innym pracownikiem opieki zdrowotnej. Decyzje
kliniczne pozostaja w gestii licencjonowanego personelu medycznego.

---

## 3. Zabronione sposoby korzystania

3.1. Zabrania sie:

a) przesylania dokumentow zawierajacych materialy nielegalne, naruszajace prawa autorskie
   lub objete tajemnica, do ktorej Tenant nie ma upowaznienia;

b) prob wyluszczenia danych treningowych modeli AI (tzw. prompt extraction lub model inversion);

c) proby ominieccia izolacji miedzy tenantami lub uzyskania dostepu do danych innego tenanta;

d) uzywania Platformy do podejmowania w pelni zautomatyzowanych decyzji dotyczacych osob
   fizycznych bez udzialu czlowieka (zakaz wynikajacy z Art. 22 RODO);

e) przesylania danych osobowych innych niz te, ktore sa niezbedne do realizacji celów,
   dla ktorych Platforma jest uzywana przez Tenanta;

f) uzywania Platformy do celow niezgodnych z prawem Unii Europejskiej lub prawem polskim.

---

## 4. Przetwarzanie danych osobowych (RODO)

4.1. Administrator danych: [Nazwa dostawcy], [adres], [kraj].

4.2. Podstawa prawna przetwarzania: Art. 6 ust. 1 lit. b RODO (wykonanie umowy) dla danych
konversacji i profili uzytkownikow; Art. 6 ust. 1 lit. c RODO (obowiazek prawny) dla logow
audytowych; Art. 9 ust. 2 lit. h RODO (ochrona zdrowia) jesli dokumenty zawieraja dane
medyczne — wymagana pisemna umowa powierzenia przetwarzania.

4.3. Prawa podmiotow danych:
- Prawo dostepu (Art. 15 RODO)
- Prawo do sprostowania (Art. 16 RODO)
- Prawo do usuniecia ("prawo do bycia zapomnianym", Art. 17 RODO) — realizowane przez
  usniecie tenanta; kaskadowe usuniecie danych z Postgres, bazy wektorowej i MinIO
- Prawo do ograniczenia przetwarzania (Art. 18 RODO)
- Prawo do przenosnosci danych (Art. 20 RODO) — eksport konwersacji w formacie JSON na zadanie
- Prawo sprzeciwu (Art. 21 RODO)

4.4. Zgloszenia naruszen: naruszenie ochrony danych osobowych zostanie zglosszone do UODO
w ciagu 72 godzin od wykrycia (Art. 33 RODO).

---

## 5. Retencja danych

5.1. Dokumenty: przechowywane przez caly okres aktywnosci kolekcji. Usuniete na zadanie
lub po usunieciu tenanta (kaskada).

5.2. Historia konwersacji: przechowywana przez 24 miesiac od daty ostatniej wiadomosci
w konwersacji. Po tym czasie automatycznie usuwana.

5.3. Logi audytowe: przechowywane zgodnie z `tenants.settings.retention_days` (domyslnie
730 dni — 2 lata). Nie moga byc usuwane na zadanie uzytkownika (wymog prawa).

5.4. Po usunieciu tenanta: wszystkie dane sa kaskadowo usuwane z Postgres, bazy wektorowej
(Qdrant) i magazynu plikow (MinIO) w ciagu 30 dni.

---

## 6. Interfejs czatu (Open WebUI)

6.1. Interfejs czatu Platformy jest oparty o oprogramowanie Open WebUI (licencja MIT/community).

6.2. Zgodnie z licencja community Open WebUI: dla wdrozen z mniej niz 50 aktywnych uzytkownikow
miesiecznie (MAU) branding Open WebUI musi pozostac widoczny w interfejsie.

6.3. Dla wdrozen z 50 lub wiecej MAU wymagana jest licencja enterprise Open WebUI umozliwiajaca
white-labelling. Dostawca poinformuje Tenanta z wyprzedzeniem (przy 40 MAU) o zbliajacym sie
progu.

6.4. Tenant akceptuje, ze czesc funkcjonalnosci frontendowej jest dostarczona przez Open WebUI
i objeta jej warunkami (dostepnymi pod: https://openwebui.com).

---

## 7. Dostepnosc i SLA

7.1. Dostawca dokola starac sie o dostepnosc systemu na poziomie 99,5% miesiecznie (SLA).

7.2. Planowane przerwy techniczne beda komunikowane z co najmniej 24-godzinnym wyprzedzeniem.

7.3. System nie ponosi odpowiedzialnosci za decyzje kliniczne podjete na podstawie odpowiedzi AI.

---

## 8. Odpowiedzialnosc

8.1. Dostawca nie ponosi odpowiedzialnosci za decyzje kliniczne, organizacyjne ani inne podjete
przez Uzytkownikow lub Tenanta na podstawie Odpowiedzi AI.

8.2. Calkowita odpowiedzialnosc Dostawcy jest ograniczona do wysokosci oplat wniesionych przez
Tenanta w poprzednich 12 miesiacach.

8.3. Dostawca nie ponosi odpowiedzialnosci za szkody wynikajace z:
a) niestosowania sie do niniejszych Warunkow korzystania,
b) bledu lub niepelnosci dokumentow dostarczonych przez Tenanta,
c) sil wyzszych (przerwy w dostawie pradzu, awarie infrastruktury chmurowej itp.).

---

## 9. Prawo wlasciwe i rozstrzyganie sporow

9.1. Niniejsze Warunki korzystania podlegaja prawu polskiemu.

9.2. Wszelkie spory beda rozstrzygane przez sad wlasciwy dla siedziby Dostawcy.

9.3. W sprawach nieuregulowanych niniejszymi Warunkami zastosowanie ma Kodeks cywilny,
ustawa o swiadczeniu uslug droga elektroniczna oraz RODO.

---

## 10. Zmiany Warunkow korzystania

10.1. Dostawca zastrzega sobie prawo do zmiany niniejszych Warunkow korzystania. O zmianach
Dostawca poinformuje Tenanta z co najmniej 14-dniowym wyprzedzeniem.

10.2. Dalsze korzystanie z Platformy po wejsciu w zycie nowej wersji Warunkow, po ich akceptacji
przez Wlasciciela tenanta, oznacza ich akceptacje.

10.3. Jesli Tenant nie zgadza sie z nowymi Warunkami, moze wypowiedziec umowe i zadac usuniecia
swoich danych (Art. 17 RODO).

---

*Kliknieciem przycisku "Akceptuje warunki korzystania" potwierdzasz, ze przeczytales/-as i
rozumiesz niniejsze Warunki korzystania oraz ze masz uprawnienia do ich akceptacji w imieniu
swojej organizacji.*
```

---

## UX Flow & Wireframe (text-based)

### Flow 1: Onboarding nowego tenanta

```
Krok 1: Rejestracja tenanta
--> Krok 2: Wyswietlenie WK (patrz wireframe A)
--> Krok 3: Akceptacja (klikniecie przycisku)
--> Krok 4: Pierwsze logowanie do panelu systemu
```

### Wireframe A: Ekran akceptacji WK (nowy tenant)

```
+------------------------------------------------------------+
| Warunki korzystania z systemu                              |
+------------------------------------------------------------+
|                                                            |
| Wersja 1.0 · Obowiazuje od: 1 lipca 2026                  |
|                                                            |
| [Ramka z trescia WK, przewijalna, min. 300px wysokosci]   |
| +--------------------------------------------------------+ |
| | # Warunki korzystania z platformy RAG                  | |
| |                                                        | |
| | ## 1. Definicje                                        | |
| | ...                                                    | |
| | ## 2. Dozwolone sposoby korzystania                    | |
| | ...                                                    | |
| | [przewij aby przeczytac dalej]                         | |
| +--------------------------------------------------------+ |
|                                                            |
| [x] Przeczytalam/-em i akceptuje warunki korzystania       |
|     z systemu oraz potwierdzam, ze mam uprawnienia do      |
|     akceptacji w imieniu mojej organizacji.                |
|                                                            |
| [ Akceptuje warunki korzystania ]  (przycisk nieaktywny   |
|                                     do zaznaczenia checkbox)|
|                                                            |
| Masz pytania? Skontaktuj sie z [adres@dostawca.pl]        |
+------------------------------------------------------------+
```

Regula UX: przycisk "Akceptuje" jest nieaktywny dopoki checkbox nie jest zaznaczony. To realizuje wymog `explicit_consent`.

### Wireframe B: Ekran ponownej akceptacji (zmiana WK)

```
+------------------------------------------------------------+
| Warunki korzystania zostaly zaktualizowane                 |
+------------------------------------------------------------+
|                                                            |
| Zaktualizowalismy warunki korzystania z systemu.           |
| Nowa wersja obowiazuje od 1 pazdziernika 2026.            |
|                                                            |
| Co sie zmienilo?                                           |
| Wersja 1.1 wprowadza:                                      |
|  - Zaktualizowany czas retencji historii konwersacji       |
|    (z 12 do 24 miesiecy)                                   |
|  - Informacje o licencji Open WebUI                        |
|                                                            |
| [Przeczytaj pelne warunki korzystania v1.1]               |
|                                                            |
| [x] Przeczytalam/-em i akceptuje nowe warunki korzystania  |
|                                                            |
| [ Akceptuje nowe warunki korzystania ]                     |
|                                                            |
| Jesli nie zgadzasz sie z nowymi warunkami, mozesz          |
| [wypowiedziec usluge i pobrac swoje dane].                 |
+------------------------------------------------------------+
```

### Stan: uzytkownik (nie Wlasciciel) przy nieodrzuconym WK

```
+------------------------------------------------------------+
| Dostep ograniczony                                         |
+------------------------------------------------------------+
|                                                            |
| Wlasciciel Twojej organizacji musi zaakceptowac           |
| warunki korzystania z systemu przed kontynuacja pracy.    |
|                                                            |
| Jesli jestes administratorem, zaloguj sie jako            |
| Wlasciciel organizacji i zaakceptuj warunki.              |
|                                                            |
| W razie problemow skontaktuj sie z:                        |
| [adres@dostawca.pl]                                        |
|                                                            |
+------------------------------------------------------------+
```

### Flow 2: Powiadomienie o zbliajacym sie progu MAU (40 uzytkownikow)

Powiadomienie wyswietlane Wlascicielowi w panelu (banner, nie blokujacy):

```
+------------------------------------------------------------+
| [i] Zbliazasz sie do limitu uzytkowanikow                 |
|     Twoja organizacja ma aktualnie 40 aktywnych           |
|     uzytkownikow w tym miesiacu.                          |
|                                                            |
|     Przy 50 uzytkownikach wymagana jest licencja           |
|     enterprise Open WebUI dla wlasnej marki.              |
|     Wiecej informacji: [openwebui.com/enterprise]         |
|                                                            |
|     [Zamknij]  [Skontaktuj sie z nami]                    |
+------------------------------------------------------------+
```

Warunek wyswietlenia: `mau_count >= 40`. Licznik MAU obliczany jest z tabeli `user_tenants` + logow konwersacji — szczegoly w sekcji "Implementation Steps".

---

## Architecture — SOLID & DRY

### Warstwa API

```
src/api/routers/tos.py            -- Router: GET /terms, POST + GET /tenants/{id}/terms/*
src/api/middleware/tos_check.py   -- Middleware: blokowanie dostepu bez akceptacji WK
src/api/schemas/tos.py            -- Pydantic schemas (ponizej)
```

### Warstwa domenowa

```
src/domain/tos_service.py         -- TosService: logika biznesowa WK
src/db/repositories/tos_repo.py   -- TosRepository: SQL dla tos_versions i tos_acceptances
```

`TosService` jest jedynym miejscem z logika wersjonowania, akceptacji i cache invalidacji. Middleware i Router nie dotykaja bazy bezposrednio.

### Cache

`TosService` odpowiada za cache Redis:
- `get_acceptance_status(tenant_id) -> bool` — CHECK cache first, DB on miss, SET cache
- `invalidate_cache(tenant_id)` — wywolywane po akceptacji i po aktywacji nowej wersji

```python
# Klucze Redis:
TOS_CACHE_KEY = "tos:tenant:{tenant_id}:accepted"   # TTL 60s, wartosc "1" lub "0"
TOS_VERSION_CACHE_KEY = "tos:current_version"        # TTL 300s, wartosc version string
```

### MAU Counter

```python
# src/domain/mau_service.py
# MauService.get_mau_count(tenant_id: UUID, month: date) -> int
# Zapytanie: COUNT DISTINCT user_id z conversations WHERE tenant_id = X
#            AND created_at >= first_day_of_month AND created_at < first_day_of_next_month
# Cache w Redis: mau:tenant:{tenant_id}:{YYYY-MM} TTL 3600s
# Wywolywany przy kazdym logowaniu Wlasciciela do sprawdzenia progu 40/50 MAU
```

---

## Implementation Steps

1. **Migracja Alembic** — stworz tabele `tos_versions` i `tos_acceptances`:
   - Polecenie: `alembic revision --autogenerate -m "add tos_versions and tos_acceptances tables"`
   - Plik: `alembic/versions/{timestamp}_add_tos_versions_and_tos_acceptances_tables.py`
   - `upgrade()` musi tworzyc tabele w kolejnosci: najpierw `tos_versions`, potem `tos_acceptances` (FK constraint).
   - `downgrade()` musi usuwac w odwrotnej kolejnosci: najpierw `tos_acceptances`, potem `tos_versions`.
   - Obie kolejnosci sa wymagane — migracja bez `downgrade()` jest odrzucana przez code review.
   - W `upgrade()` dodaj rowniez seed: INSERT pierwszej wersji WK (v1.0) ze statusem 'active'

2. **TosRepository** — stworz `src/db/repositories/tos_repo.py`:
   - `get_active_version() -> TosVersion | None`
   - `get_acceptance(tenant_id, tos_version_id) -> TosAcceptance | None`
   - `create_acceptance(tenant_id, user_id, tos_version_id, ip, user_agent) -> TosAcceptance`
   - `has_accepted_current_tos(tenant_id) -> bool` (uzywa zapytania z sekcji Database Patterns)

3. **TosService** — stworz `src/domain/tos_service.py`:
   - `get_current_tos() -> TosVersion`
   - `accept_tos(tenant_id, user_id, tos_version_id, ip, user_agent, explicit_consent) -> TosAcceptanceRecord`
   - `get_tos_status(tenant_id) -> TosStatusResponse`
   - `check_tenant_acceptance(tenant_id) -> bool` (Redis cache + DB fallback)
   - `invalidate_acceptance_cache(tenant_id)` (wywolywany po akceptacji)

4. **Router** — stworz `src/api/routers/tos.py`, zarejestruj w `src/api/main.py`.

5. **Middleware** — stworz `src/api/middleware/tos_check.py`:
   - Lista wykluczonych sciezek jako stala (`EXCLUDED_PATHS: frozenset[str]`)
   - Detekcja API key przez naglowek `X-Api-Key` (bez weryfikacji wartosci — to robi auth middleware)
   - Ekstrakcja `tenant_id` z JWT payload bez pelnej weryfikacji (middleware jest przed auth)
   - Jesli JWT brak lub niepoprawny: `call_next(request)` — auth middleware obsluzy 401
   - Jesli `tenant_id` brak w JWT: `call_next(request)`

   **UWAGA BEZPIECZENSTWA — niezweryfikowany JWT w middleware:**
   Middleware odczytuje `tenant_id` z niezweryfikowanego payloadu JWT wylacznie w celu sprawdzenia cache Redis. Gwarancja bezpieczenstwa: pelna weryfikacja podpisu JWT nastepuje w `get_current_ctx` (TASK-003) przy kazdym chronionym endpoincie, zanim jakakolwiek logika biznesowa zostanie wykonana. Nieveryfikowany claim w middleware sluzy tylko do okreslenia klucza cache — nie moze nadac uprawnien. Atakujacy z sfałszowanym `tenant_id` w JWT i tak nie przejdzie weryfikacji podpisu w TASK-003.

6. **Rejestracja middleware** w `src/api/main.py`:

   ```python
   # Kolejnosc ma znaczenie: TosCheck musi byc po SecurityHeadersMiddleware
   # ale przed CORSMiddleware (CORS musi byc ostatni)
   app.add_middleware(TosCheckMiddleware, redis=redis_client, tos_service=tos_service)
   ```

7. **MAU Service** — stworz `src/domain/mau_service.py`:
   - `get_mau_count(tenant_id, month) -> int`
   - `should_show_mau_warning(tenant_id) -> bool` (mau >= 40)
   - Wywolywany przez endpoint GET /tenants/{id}/terms/status (dodaj `mau_warning` do odpowiedzi)

8. **Seed danych** — w ramach migracji Alembic lub oddzielnego skryptu `scripts/seed_tos_v1.py`:
   - Wstaw WK v1.0 z trescia z sekcji "Tresc Warunkow Korzystania"
   - Ustaw `status = 'active'`, `effective_date = '2026-07-01'`

9. **DeletionService integration (GDPR Art. 17)** — BLOCKER przed mergem:

   W `src/domain/deletion_service.py` w metodzie `delete_tenant()` dodaj krok usuwania akceptacji WK **przed** usunieciem wiersza tenanta (FK constraint):

   ```python
   # Inside delete_tenant(), between Step 4 (remove user_tenants) and Step 5 (soft-delete tenant):
   # Step 4b: Delete ToS acceptances for this tenant (GDPR Art. 17 cascade)
   await self._db.execute(
       delete(TosAcceptance).where(TosAcceptance.tenant_id == tenant_id)
   )
   # Note: tos_acceptances has ON DELETE CASCADE on tenant_id FK, so this is
   # explicit for clarity and to count deleted rows in DeletionReport.
   ```

   Dodaj `deleted_tos_acceptances: int = 0` do `DeletionReport`:

   ```python
   class DeletionReport(BaseModel):
       deleted_documents: int = 0
       deleted_chunks: int = 0
       deleted_qdrant_points: int = 0
       deleted_minio_objects: int = 0
       deleted_tos_acceptances: int = 0   # NEW — required by TASK-015
       failed_documents: int = 0
       errors: list[DeletionError] = Field(default_factory=list)
   ```

   Jesli w przyszlosci zostanie dodana metoda `delete_user()` w DeletionService, musi ona rowniez usunac:
   ```sql
   DELETE FROM tos_acceptances WHERE user_id = :user_id
   ```

   Tabela `tos_acceptances` musi byc uwzgledniona w inwentarzu danych RODO (`docs/rodo.md`):

   | Tabela | Dane osobowe | Podstawa prawna | Retencja | Kaskada przy DELETE tenanta |
   |---|---|---|---|---|
   | `tos_acceptances` | `user_id`, `ip_address`, `user_agent` | Art. 7(1) RODO (dowod zgody) | Czas zycia tenanta + 2 lata (wymog audytowy) | Tak — `DeletionService.delete_tenant()` + FK CASCADE |

10. **Aktualizacja docs/03-Specyfikacja-API.md** — wymagane przed mergem.

---

## Security Checklist

- [ ] Middleware ekstrahuje `tenant_id` z JWT bez wywolania zewnetrznych serwisow (nie ma HTTP call do Keycloak w hotpath kazdego zadania)
- [ ] Akceptacja WK wymaga roli Owner — sprawdzana jawnie w serwisie, nie tylko przez Depends (resource-level authorization)
- [ ] `ip_address` zapisywany z `request.client.host` lub parsowany z `X-Forwarded-For` (konfiguracja: ile proksi jest przed API); brak walidacji = ryzyko IP spoofing — uzyj `trusted_hosts` z konfiguracji
- [ ] `ip_address` w API response jest maskowany ("192.168.1.xxx") — pelne IP tylko w bazie danych i logach audytowych (RODO: minimalizacja danych w API)
- [ ] Cache Redis nie moze byc jedynym zrodlem prawdy — po cache miss zawsze query DB
- [ ] Zmiana aktywnej wersji WK (przez superadmina) musi invalidowac cache dla WSZYSTKICH tenantow: `SCAN tos:tenant:*:accepted` + `DEL` (lub prefix-based key delete)
- [ ] Wyjatki dla kluczy API (`X-Api-Key`) sa udokumentowane i audytowane; klucze API nie moga byc uzywane do akceptacji WK
- [ ] `explicit_consent: false` zwraca 422, nie 400 — aby frontend mogl parsowac error model
- [ ] Audit log: akceptacja WK jest wpisem w `audit_log` z `ip_address` (pelnym, nie zamaskowanym) — wymagane przez RODO Art. 7 ust. 1 (mozliwosc wykazania zgody)
- [ ] Test: middleware nie blokuje `GET /health` ani `GET /metrics` (endpointy bez JWT)
- [ ] Test: middleware nie blokuje zadania z `X-Api-Key` nawet bez WK

---

## Terms of Use (relevant constraints)

Sekcja ta opisuje ograniczenia i wyjatki wynikajace z WK i licencji Open WebUI, ktore wplywaja na implementacje:

### Wyjatki dla kluczy API

Klucze API (`X-Api-Key`) omijaja sprawdzanie WK. Uzasadnienie: systemy automatyczne (np. RPA, integracje HIS/EMR szpitala) dzialaja bez sesji uzytkownika. Byloby niemozliwe, aby klikac "Akceptuje" w nazwie systemu-klienta.

**Wymagania dla wyjatku:**

- Wyjatki musza byc udokumentowane w umowie z tenantami (Wlasciciel organizacji akceptuje WK takze w imieniu kluczy API wydanych przez jego organizacje — zaznaczono w tresci WK w sekcji "Dozwolone sposoby korzystania").
- Klucze API moga byc wydane tylko po akceptacji WK przez Wlasciciela.
- Klucze API nie maja dostpu do endpointow wymagajacych roli Owner (np. `/tenants/{id}/terms/accept`).
- Kazde zadanie z kluczem API jest logowane w `audit_log` z `action = 'api_key.request'`.

### Open WebUI MAU threshold

- Przy **40 MAU**: powiadomienie w panelu Wlasciciela (banner, nie blokujacy), komunikat: "Zbliazasz sie do limitu 50 uzytkownikow. Przy przekroczeniu limitu wymagana jest licencja enterprise."
- Przy **50 MAU**: powiadomienie blokujace z wymogiem podjecia dzialania (upgrade licencji lub ograniczenie liczby uzytkownikow). System NIE blokuje automatycznie dostepu uzytkownikow — Wlasciciel musi podjac decyzje.
- MAU jest liczony jako liczba unikalnych `user_id` z przynajmniej jedna konwersacja w ciagu ostatnich 30 dni kalendarzowych.
- Przynaleznosc do MAU: uzytkownik liczony jest od momentu wyslania pierwszej wiadomosci w danym miesiacu.

### Retencja danych (implementacja backendowa)

- Historia konwersacji: nalezy zaimplementowac cron job (np. Celery beat) uruchamiajacy co noc `DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE updated_at < now() - interval '24 months')`.
- Trigger GDPR Art. 17: DELETE tenanta musi wywolac `DeletionService.delete_tenant()` obejmujacy Postgres (kaskada przez FK), Qdrant (filter by `tenant_id`), MinIO (prefix `tenant-{slug}/`), Redis (flush kluczy `tos:tenant:{id}:*`).
- Dokumentacja kaskady musi byc zaktualizowana w `docs/rodo.md` po implementacji tego tasku.

---

## Tests

### Testy jednostkowe

**`tests/unit/tos/test_tos_service.py`**

```python
# Wymagane przypadki testowe:

# 1. get_current_tos — zwraca aktywna wersje z DB (po cache miss)
# 2. get_current_tos — zwraca wersje z cache Redis (bez zapytania DB)
# 3. accept_tos — tworzy rekord akceptacji, zwraca TosAcceptanceRecord
# 4. accept_tos — idempotentne: drugi call zwraca istniejacy rekord
# 5. accept_tos — rzuca blad jesli explicit_consent=False
# 6. accept_tos — rzuca blad jesli tos_version_id nie odpowiada aktywnej wersji
# 7. accept_tos — invaliduje cache po akceptacji
# 8. check_tenant_acceptance — True po akceptacji
# 9. check_tenant_acceptance — False dla nowego tenanta
# 10. check_tenant_acceptance — False po aktywacji nowej wersji WK (stara akceptacja)
```

**`tests/unit/tos/test_tos_middleware.py`**

```python
# 11. Middleware przepuszcza zadania do GET /terms bez sprawdzania akceptacji
# 12. Middleware przepuszcza zadania z X-Api-Key bez sprawdzania akceptacji
# 13. Middleware zwraca 403 z tos_required=true jesli tenant nie zaakceptowal
# 14. Middleware przepuszcza jesli tenant zaakceptowal (cache hit)
# 15. Middleware przepuszcza jesli brak JWT (deleguje 401 do auth middleware)
# 16. 403 response zawiera pole accept_url z poprawnym URL tenanta
```

### Testy integracyjne

**`tests/integration/test_tos_endpoints.py`**

```python
# GET /terms
# - 200 z aktywna wersja WK
# - 404 jesli brak aktywnej wersji

# POST /tenants/{id}/terms/accept
# - 200 dla Wlasciciela z poprawnymi danymi
# - 200 idempotentne (drugi call z tymi samymi danymi)
# - 403 dla roli Admin (nie Owner)
# - 403 dla roli Contributor
# - 422 dla explicit_consent=false
# - 422 dla tos_version_id nieodpowiadajacego aktywnej wersji
# - Wpis w audit_log po akceptacji

# GET /tenants/{id}/terms/status
# - 200 z is_accepted=true po akceptacji
# - 200 z is_accepted=false + requires_reacceptance=true po aktywacji nowej wersji
# - 403 dla Viewer (brak roli Admin/Owner)

# Middleware integration:
# - Dowolny chroniony endpoint zwraca 403 z tos_required=true dla tenanta bez akceptacji
# - Ten sam endpoint zwraca 200 po akceptacji
# - X-Api-Key omija middleware (200 nawet bez akceptacji WK przez tenanta)
```

### Testy RODO / audit

```python
# test_tos_acceptance_creates_audit_log:
#   - po POST /tenants/{id}/terms/accept sprawdz audit_log
#   - action == 'tos.accepted'
#   - details zawiera version i ip_address

# test_deletion_cascade_removes_tos_acceptances:
#   - usniecie tenanta kaskaduje do tos_acceptances (FK ON DELETE CASCADE)
#   - po usunieciu tenanta brak rekordow w tos_acceptances dla tego tenant_id
```

---

## Definition of Done

- [ ] Migracja Alembic: tabele `tos_versions` i `tos_acceptances` z seedem v1.0
- [ ] Tresc WK v1.0 wprowadzona do bazy (seed w migracji lub skrypt inicjalizacyjny)
- [ ] Wszystkie 3 endpointy zaimplementowane i przetestowane
- [ ] Middleware `TosCheckMiddleware` zarejestrowany i dzialajacy
- [ ] Cache Redis dla statusu akceptacji dziala (test: DB nie jest pytana jesli cache hit)
- [ ] MAU counter zaimplementowany; powiadomienie przy 40 MAU widoczne w GET /tenants/{id}/terms/status
- [ ] Test: `explicit_consent=false` → 422 z czytelnym komunikatem po polsku
- [ ] Test: ponowna akceptacja tej samej wersji → idempotentna odpowiedz 200
- [ ] Test: middleware nie blokuje `/health`, `/metrics`, `/terms`, `/auth/*`
- [ ] Test: middleware blokuje (403 + `tos_required=true`) chroniony endpoint dla tenanta bez akceptacji
- [ ] Test: X-Api-Key omija middleware
- [ ] `docs/03-Specyfikacja-API.md` zaktualizowany o nowe endpointy
- [ ] `docs/rodo.md` zaktualizowany o: retencje konwersacji 24m, kaskade WK przy DELETE tenanta
- [ ] Tresc WK zaakceptowana przez radce prawnego (uwaga: to jest wymog poza kodem — oznaczyc jako TODO az do weryfikacji prawnej)
- [ ] `python-reviewer` (profil security) zatwierdzil PR
- [ ] `security-auditor` zatwierdzil po sprawdzeniu listy z sekcji Security Checklist
