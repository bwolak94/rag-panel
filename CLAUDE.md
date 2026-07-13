# RAG Platform — project memory (CLAUDE.md)

> Place this file in the **repository root**. The `.claude/` folder belongs next to it.

## About the project

Self-hosted, multi-tenant RAG platform (pilot: medical clinic, eventually any organisation).
Stack: Python 3.12, FastAPI, LangGraph + LangChain, Qdrant, PostgreSQL 16, MinIO (event-driven ingest via Redis Streams), Ollama/vLLM, Open WebUI (frontend), Keycloak (OIDC), Langfuse.

Project documentation: `docs/` — PRD, Architecture (ADRs!), API Specification, Data Model, Security-GDPR, Roadmap. **Always read the relevant document before implementing and update it after changes.**

## Reguły (importy)

@.claude/rules/coding-standards.md
@.claude/rules/security.md
@.claude/rules/rag-conventions.md

## Repository structure (target)

```
src/
  api/            # FastAPI: routers, Pydantic schemas, dependencies (auth, RBAC)
  core/           # config, logging, exceptions, clients (qdrant, minio, redis)
  domain/         # domain models, business services
  graphs/         # LangGraph: query_graph/, ingest_graph/ (nodes = separate files)
  ingest/         # worker, extraction, chunking, embeddings
  retrieval/      # RetrievalService — THE ONLY module with Qdrant access
  db/             # SQLAlchemy models, Alembic migrations
tests/            # unit/, integration/ (testcontainers), eval/ (RAGAS)
docs/             # project documents
```

## How we work — agent orchestration

Sub-agents are available in `.claude/agents/`. Delegate according to this scheme:

1. **New feature:** `architect` (decision/ADR) → `data-engineer` (data model) → `backend-dev` or `rag-engineer` (implementation) → `python-reviewer` (review) → `security-auditor` (if auth/GDPR is involved) → for API changes: `design`.
2. **RAG pipeline / prompts / evaluation work:** `rag-engineer` + `ml-engineer` (model selection, metrics).
3. **Inter-service communication / deployment changes:** `microservices`.
4. **DB schema changes:** `data-engineer` (model + migration) → `backend-dev` (service/repo).
5. **auth/RBAC/retrieval changes:** always finish with `security-auditor` + skill `/tenant-isolation-check`.
6. Pass the output of one agent to the next in the prompt (agents cannot see each other's context).
7. Conflicts are resolved by `architect`; record decisions as ADRs in `docs/02-Architektura.md`.

## Skills (procedures)

| Skill                     | When to use                                                    |
|---------------------------|----------------------------------------------------------------|
| `/new-endpoint`           | New REST endpoint (RBAC, audit log, tests, docs)               |
| `/langgraph-node`         | New or modified query or ingest graph node                     |
| `/ingest-stage`           | Ingest pipeline change (extraction, chunking, embedding)       |
| `/prompt-version`         | Any prompt content change (versioning + changelog)             |
| `/rag-eval`               | After prompt/retrieval/chunking/model changes                  |
| `/debug-rag`              | Poor responses, hallucinations, no results                     |
| `/tenant-isolation-check` | After auth/RBAC/retrieval changes; merge blocker               |
| `/rodo-audit`             | Before release; new data type; new PII flow                    |
| `/deletion-cascade`       | New data type → implement GDPR Art. 17 handling                |
| `/db-migration`           | Any Postgres schema change                                     |
| `/reindex-collection`     | Embedding model change for a Qdrant collection                 |
| `/model-registry-update`  | New LLM or embedding model (Ollama/vLLM)                       |
| `/add-trace`              | New component needs Langfuse tracing                           |
| `/adr`                    | Before implementation — document the architectural decision    |
| `/commit`                 | Commit with lint + mypy + tests (Conventional Commits)         |

## Commands

- Tests: `pytest -x -q` (integration: `pytest tests/integration -m integration`)
- Lint/format: `ruff check --fix . && ruff format .`
- Types: `mypy src/`
- Local stack: `docker compose up -d`
- Migrations: `alembic upgrade head` / `alembic revision --autogenerate -m "..."`

## Hard rules (never break these)

1. **No code without documentation coverage** — if a decision is missing, write the ADR first.
2. Every Qdrant query goes through `RetrievalService` with a `tenant_id` filter — no exceptions.
3. Tenant isolation tests must pass before every merge (blocker).
4. No secrets in code/commits; configuration via env (pydantic-settings).
5. Prompt content and user responses must not appear in application logs.
6. New endpoint = entry in `docs/03-Specyfikacja-API.md` + per-role authorization test.
