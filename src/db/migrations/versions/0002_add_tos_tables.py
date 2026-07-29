# ruff: noqa: E501
"""add_tos_tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-29

Creates `tos_versions` and `tos_acceptances` tables and seeds ToS v1.0
with status='active'.

Design notes:
- `tos_versions.created_by` is a plain UUID, NOT a FK to `users`.  The
  system admin seeding initial ToS content may not have a user record.
- A partial unique index enforces "only one active version at a time".
- `tos_acceptances.ip_address` uses VARCHAR(45) (covers IPv4 + IPv6)
  instead of Postgres INET for portability across async drivers.
- The seed INSERT uses a fixed UUID so downgrade() can DELETE it safely.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_TOS_ID = "00000000-0000-0000-0000-000000000001"
_SEED_CREATED_BY = "00000000-0000-0000-0000-000000000000"

_TOS_V1_CONTENT = """\
# Warunki korzystania z platformy RAG

**Wersja 1.0**
**Data wejscia w zycie: 1 lipca 2026**
**Dostawca:** [Nazwa dostawcy systemu]
**Kontakt:** [adres e-mail dostawcy]

---

## 1. Definicje

**Platforma** oznacza system informatyczny oparty na technologii RAG (Retrieval-Augmented Generation), udostepniany przez Dostawce w trybie SaaS lub self-hosted.

**Tenant** oznacza organizacje (np. przychodnie, szpital, przedsiebiorstwo) korzystajaca z Platformy na podstawie odrebnej umowy lub rejestracji.

**Uzytkownik** oznacza osobe fizyczna korzystajaca z Platformy w imieniu Tenanta.

**Dokumenty** oznaczaja pliki przesylane przez Uzytkownikow w celu indeksowania i wyszukiwania.

**Odpowiedzi AI** oznaczaja wyniki generowane przez modele jezykowe na podstawie Dokumentow.

---

## 2. Dozwolone sposoby korzystania

2.1. Platforma jest przeznaczona wylacznie do: wyszukiwania informacji w dokumentach organizacji, wspomaganego przez AI przeszukiwania baz wiedzy, uzyskiwania odpowiedzi na pytania na podstawie wgraznych dokumentow.

2.2. Odpowiedzi AI maja charakter informacyjny. Nie stanowia porady medycznej, prawnej ani zadnej innej porady profesjonalnej. Uzytkownik jest zobowiazany do weryfikacji odpowiedzi AI z odpowiednim specjalista przed podjaciem decyzji.

2.3. W kontekscie medycznym: odpowiedzi Platformy NIE zastepuja diagnozy lekarskiej, decyzji klinicznej ani konsultacji z lekarzem lub innym pracownikiem opieki zdrowotnej. Decyzje kliniczne pozostaja w gestii licencjonowanego personelu medycznego.

---

## 3. Zabronione sposoby korzystania

3.1. Zabrania sie: a) przesylania dokumentow zawierajacych materialy nielegalne, naruszajace prawa autorskie lub objete tajemnica, do ktorej Tenant nie ma upowaznienia; b) prob wyluszczenia danych treningowych modeli AI (tzw. prompt extraction lub model inversion); c) proby ominieccia izolacji miedzy tenantami lub uzyskania dostepu do danych innego tenanta; d) uzywania Platformy do podejmowania w pelni zautomatyzowanych decyzji dotyczacych osob fizycznych bez udzialu czlowieka (zakaz wynikajacy z Art. 22 RODO); e) przesylania danych osobowych innych niz te, ktore sa niezbedne do realizacji celow, dla ktorych Platforma jest uzywana przez Tenanta; f) uzywania Platformy do celow niezgodnych z prawem Unii Europejskiej lub prawem polskim.

---

## 4. Przetwarzanie danych osobowych (RODO)

4.1. Administrator danych: [Nazwa dostawcy], [adres], [kraj].

4.2. Podstawa prawna przetwarzania: Art. 6 ust. 1 lit. b RODO (wykonanie umowy) dla danych konversacji i profili uzytkownikow; Art. 6 ust. 1 lit. c RODO (obowiazek prawny) dla logow audytowych; Art. 9 ust. 2 lit. h RODO (ochrona zdrowia) jesli dokumenty zawieraja dane medyczne — wymagana pisemna umowa powierzenia przetwarzania.

4.3. Prawa podmiotow danych: Prawo dostepu (Art. 15 RODO), Prawo do sprostowania (Art. 16 RODO), Prawo do usuniecia ("prawo do bycia zapomnianym", Art. 17 RODO) — realizowane przez usniecie tenanta; kaskadowe usuniecie danych z Postgres, bazy wektorowej i MinIO, Prawo do ograniczenia przetwarzania (Art. 18 RODO), Prawo do przenosnosci danych (Art. 20 RODO) — eksport konwersacji w formacie JSON na zadanie, Prawo sprzeciwu (Art. 21 RODO).

4.4. Zgloszenia naruszen: naruszenie ochrony danych osobowych zostanie zglosszone do UODO w ciagu 72 godzin od wykrycia (Art. 33 RODO).

---

## 5. Retencja danych

5.1. Dokumenty: przechowywane przez caly okres aktywnosci kolekcji. Usuniete na zadanie lub po usunieciu tenanta (kaskada).

5.2. Historia konwersacji: przechowywana przez 24 miesiac od daty ostatniej wiadomosci w konwersacji. Po tym czasie automatycznie usuwana.

5.3. Logi audytowe: przechowywane zgodnie z tenants.settings.retention_days (domyslnie 730 dni — 2 lata). Nie moga byc usuwane na zadanie uzytkownika (wymog prawa).

5.4. Po usunieciu tenanta: wszystkie dane sa kaskadowo usuwane z Postgres, bazy wektorowej (Qdrant) i magazynu plikow (MinIO) w ciagu 30 dni.

---

## 6. Interfejs czatu (Open WebUI)

6.1. Interfejs czatu Platformy jest oparty o oprogramowanie Open WebUI (licencja MIT/community).

6.2. Zgodnie z licencja community Open WebUI: dla wdrozen z mniej niz 50 aktywnych uzytkownikow miesiecznie (MAU) branding Open WebUI musi pozostac widoczny w interfejsie.

6.3. Dla wdrozen z 50 lub wiecej MAU wymagana jest licencja enterprise Open WebUI umozliwiajaca white-labelling. Dostawca poinformuje Tenanta z wyprzedzeniem (przy 40 MAU) o zbliajacym sie progu.

6.4. Tenant akceptuje, ze czesc funkcjonalnosci frontendowej jest dostarczona przez Open WebUI i objeta jej warunkami.

---

## 7. Dostepnosc i SLA

7.1. Dostawca dokola starac sie o dostepnosc systemu na poziomie 99,5% miesiecznie (SLA).

7.2. Planowane przerwy techniczne beda komunikowane z co najmniej 24-godzinnym wyprzedzeniem.

7.3. System nie ponosi odpowiedzialnosci za decyzje kliniczne podjete na podstawie odpowiedzi AI.

---

## 8. Odpowiedzialnosc

8.1. Dostawca nie ponosi odpowiedzialnosci za decyzje kliniczne, organizacyjne ani inne podjete przez Uzytkownikow lub Tenanta na podstawie Odpowiedzi AI.

8.2. Calkowita odpowiedzialnosc Dostawcy jest ograniczona do wysokosci oplat wniesionych przez Tenanta w poprzednich 12 miesiacach.

8.3. Dostawca nie ponosi odpowiedzialnosci za szkody wynikajace z: a) niestosowania sie do niniejszych Warunkow korzystania, b) bledu lub niepelnosci dokumentow dostarczonych przez Tenanta, c) sil wyzszych (przerwy w dostawie pradzu, awarie infrastruktury chmurowej itp.).

---

## 9. Prawo wlasciwe i rozstrzyganie sporow

9.1. Niniejsze Warunki korzystania podlegaja prawu polskiemu.

9.2. Wszelkie spory beda rozstrzygane przez sad wlasciwy dla siedziby Dostawcy.

9.3. W sprawach nieuregulowanych niniejszymi Warunkami zastosowanie ma Kodeks cywilny, ustawa o swiadczeniu uslug droga elektroniczna oraz RODO.

---

## 10. Zmiany Warunkow korzystania

10.1. Dostawca zastrzega sobie prawo do zmiany niniejszych Warunkow korzystania. O zmianach Dostawca poinformuje Tenanta z co najmniej 14-dniowym wyprzedzeniem.

10.2. Dalsze korzystanie z Platformy po wejsciu w zycie nowej wersji Warunkow, po ich akceptacji przez Wlasciciela tenanta, oznacza ich akceptacje.

10.3. Jesli Tenant nie zgadza sie z nowymi Warunkami, moze wypowiedziec umowe i zadac usuniecia swoich danych (Art. 17 RODO).

---

*Klknieciem przycisku "Akceptuje warunki korzystania" potwierdzasz, ze przeczytales/-as i rozumiesz niniejsze Warunki korzystania oraz ze masz uprawnienia do ich akceptacji w imieniu swojej organizacji.*\
"""

_TOS_V1_SUMMARY = "Wersja poczatkowa warunkow korzystania z platformy RAG."


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # tos_versions (no FK deps)                                           #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tos_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("effective_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("version", name="uq_tos_versions_version"),
    )

    # Partial unique index: only one version may have status='active' at a time.
    op.create_index(
        "tos_versions_single_active",
        "tos_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # ------------------------------------------------------------------ #
    # tos_acceptances (FKs: tenants, users, tos_versions)                 #
    # ------------------------------------------------------------------ #
    op.create_table(
        "tos_acceptances",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "tos_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tos_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(45), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index(
        "tos_acceptances_tenant_version",
        "tos_acceptances",
        ["tenant_id", "tos_version_id"],
    )
    op.create_index(
        "tos_acceptances_tenant_id",
        "tos_acceptances",
        ["tenant_id"],
    )

    # ------------------------------------------------------------------ #
    # Seed: ToS v1.0 (active)                                             #
    # ------------------------------------------------------------------ #
    op.execute(
        sa.text(
            """
            INSERT INTO tos_versions (
                id,
                version,
                content,
                summary,
                status,
                effective_date,
                created_by,
                created_at,
                updated_at
            ) VALUES (
                :id,
                :version,
                :content,
                :summary,
                'active',
                :effective_date,
                :created_by,
                now(),
                now()
            )
            """
        ).bindparams(
            id=_SEED_TOS_ID,
            version="1.0",
            content=_TOS_V1_CONTENT,
            summary=_TOS_V1_SUMMARY,
            effective_date="2026-07-01 00:00:00+00",
            created_by=_SEED_CREATED_BY,
        )
    )


def downgrade() -> None:
    # Delete seed first so the partial unique index is not blocking the drop.
    op.execute(
        sa.text("DELETE FROM tos_versions WHERE version = '1.0'")
    )

    op.drop_index("tos_acceptances_tenant_id", table_name="tos_acceptances")
    op.drop_index("tos_acceptances_tenant_version", table_name="tos_acceptances")
    op.drop_table("tos_acceptances")

    op.drop_index("tos_versions_single_active", table_name="tos_versions")
    op.drop_table("tos_versions")
