# Instalacja konfiguracji Claude Code

## Struktura

```
twoje-repo/
├── CLAUDE.md                    ← z tego pakietu (pamięć projektu + importy reguł)
├── docs/                        ← skopiuj dokumenty projektowe (PRD, Architektura, API, ...)
└── .claude/                     ← zmień nazwę folderu dot-claude → .claude
    ├── settings.json            ← uprawnienia (współdzielone w repo)
    ├── agents/                  ← 7 subagentów
    │   ├── architect.md         (decyzje, ADR-y — wołaj najpierw)
    │   ├── design.md            (UX, kontrakty API, flow, teksty)
    │   ├── ml-engineer.md       (modele, benchmarki, ewaluacja)
    │   ├── rag-engineer.md      (LangGraph, retrieval, prompty)
    │   ├── backend-dev.md       (FastAPI, Postgres, integracje)
    │   ├── microservices.md     (kolejki, Docker/K8s, observability)
    │   └── python-reviewer.md   (review — wołaj na końcu)
    ├── skills/                  ← 4 procedury (uruchamiane jako /nazwa lub automatycznie)
    │   ├── new-endpoint/
    │   ├── langgraph-node/
    │   ├── rag-eval/
    │   └── tenant-isolation-check/
    └── rules/                   ← importowane przez CLAUDE.md
        ├── coding-standards.md
        ├── security.md
        └── rag-conventions.md
```

## Kroki

1. Rozpakuj archiwum w korzeniu repo.
2. Zmień nazwę `dot-claude` → `.claude` (jeśli rozpakowałeś z zip-a `claude-config.zip`, folder ma już poprawną nazwę).
3. `CLAUDE.md` zostaw w korzeniu repo; skopiuj dokumenty projektowe do `docs/`.
4. Sprawdź: `claude` → `/agents` powinno pokazać 7 agentów, `/skills` (lub wpisanie `/new-endpoint`) — skille.

## Jak z tego korzystać (przepływ pracy)

- **Nowa funkcjonalność:** "Użyj agenta architect, żeby zdecydować jak zrobić X" → wynik przekaż: "backend-dev: zaimplementuj wg tej decyzji" → "python-reviewer: zrób review zmian".
- **Zmiany w RAG:** rag-engineer (implementacja) + skill `/rag-eval` po zmianach promptów.
- **Przed release:** `/tenant-isolation-check`.
- Agenci nie widzą nawzajem swoich rozmów — wyniki jednego wklejaj do promptu następnego (albo pozwól głównemu wątkowi Claude Code orkiestrować: opisz zadanie, a on sam deleguje wg CLAUDE.md).

## Dostosowanie

- Model agentów: architect ma `model: opus` (droższy, lepszy do decyzji); pozostali dziedziczą Twój domyślny. Zmień w frontmatter wg budżetu.
- `settings.json` — lista dozwolonych komend Bash; rozszerz wg potrzeb.
