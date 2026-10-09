# RAG Platform

[![CI](https://github.com/bwolak94/rag-panel/actions/workflows/ci.yml/badge.svg)](https://github.com/bwolak94/rag-panel/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-orchestration-1C3C3C)
![Qdrant](https://img.shields.io/badge/Qdrant-vector%20store-DC244C)
![Keycloak](https://img.shields.io/badge/Keycloak-OIDC%20%2F%20RBAC-4D4D4D?logo=keycloak&logoColor=white)
![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64)
![License: Proprietary](https://img.shields.io/badge/license-proprietary-lightgrey)

A self-hosted, multi-tenant Retrieval-Augmented Generation (RAG) platform built for organisations handling sensitive documents. Designed on-premise-first (initial pilot: medical clinic), with a generic multi-tenant architecture that can be white-labelled for any organisation.

---

## Key Features

- **Multi-tenant isolation** — every query, chunk, and file is scoped to a `tenant_id`; tenants cannot see each other's data
- **Event-driven document ingest** — upload triggers an async LangGraph pipeline: extraction, PII scan, deduplication, chunking, embedding, and indexing
- **LangGraph query pipeline** — 7-node graph: intent classification, query rewrite, vector retrieval, document grading, generation with citations, output guardrails, and persistence
- **RBAC via Keycloak (OIDC/SSO)** — roles per tenant (`admin`, `worker`, `user`); permissions enforced at both API and retrieval layer
- **OpenAI-compatible API** — works as a drop-in backend for Open WebUI and any OpenAI SDK client
- **Swappable LLM/embedding models** — all AI calls go through an abstract OpenAI-protocol client; model change = registry entry, not code change
- **Idempotent ingest** — SHA-256 deduplication + deterministic Qdrant point IDs; reprocessing is safe
- **Full observability** — Langfuse tracing (PII-masked), Prometheus metrics, structured logs (structlog)
- **GDPR / medical-grade data handling** — no data leaves the infrastructure; cascading deletion (Postgres -> Qdrant -> MinIO)

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI (async), Pydantic v2 |
| Graph orchestration | LangGraph + LangChain |
| Vector store | Qdrant |
| Relational DB | PostgreSQL 16 (SQLAlchemy async + Alembic) |
| Object storage | MinIO |
| Event queue | Redis Streams (consumer groups) |
| LLM / embeddings | Ollama (dev), vLLM (production), OpenAI-compatible |
| Identity | Keycloak (OIDC, SSO, MFA) |
| Frontend | Open WebUI |
| Observability | Langfuse, Prometheus, Grafana, structlog |
| Packaging | uv, Docker Compose / Helm (K8s) |

---

## Architecture

### High-Level Overview

```
+-----------------------------------------------------------------------+
|                          User / Browser                               |
+------------------------------------+----------------------------------+
                                     | HTTPS
                         +-----------v-----------+
                         |       Open WebUI      |  chat frontend
                         +-----------+-----------+
                            OIDC     |  OpenAI-compatible API + JWT
               +-----------+         |
               | Keycloak  |<--------+
               +-----------+         |
                         +-----------v-----------+
                         |       RAG API         |  FastAPI, stateless
                         |      (FastAPI)        |  RBAC middleware
                         +---+----------+--------+
                             |          |
             +---------------v--+  +----v--------------+
             |   Query Graph    |  |   MinIO upload    |
             |   (LangGraph)    |  +----+---------------+
             +--+---------------+       | bucket notification
                |                  +----v--------+
                |                  |   Redis     |  event stream
                |                  |  Streams    |
                |                  +----+--------+
                |                  +----v------------------+
                |                  |  Ingest Worker        |  consumer group
                |                  |  (LangGraph)          |
                |                  +----+------------------+
                |                       |
         +------v-----------------------v------+
         |           Data Stores               |
         |  PostgreSQL    Qdrant    MinIO       |
         +------+------------------------------+
                |
         +------v------------------+
         |    Ollama / vLLM        |  GPU host
         +-------------------------+
```

### Full Component Diagram (Mermaid)

```mermaid
graph TB
    subgraph "External"
        USER[User / Browser]
    end

    subgraph "Frontend Layer"
        OWUI[Open WebUI]
    end

    subgraph "Identity"
        KC[Keycloak OIDC / SSO]
    end

    subgraph "API Layer"
        API[RAG API FastAPI, stateless]
    end

    subgraph "Processing Layer"
        WORKER[Ingest Worker Redis consumer group]
    end

    subgraph "Orchestration"
        QG[LangGraph Query Graph]
        IG[LangGraph Ingest Graph]
    end

    subgraph "Data Stores"
        PG[(PostgreSQL 16)]
        QD[(Qdrant vector store)]
        MINIO[(MinIO object storage)]
        REDIS[(Redis Streams event queue)]
    end

    subgraph "AI / GPU Host"
        LLM[Ollama / vLLM LLM + embeddings]
    end

    subgraph "Observability"
        LF[Langfuse LLM tracing]
        PROM[Prometheus + Grafana]
    end

    USER -->|HTTPS| OWUI
    OWUI -->|OIDC login| KC
    OWUI -->|OpenAI API + Bearer JWT| API

    API -->|JWT verification JWKS| KC
    API -->|read/write| PG
    API -->|upload files| MINIO
    API -->|orchestrate query| QG
    API -->|traces| LF
    API -->|metrics| PROM

    QG -->|retrieve via RetrievalService| QD
    QG -->|LLM calls| LLM
    QG -->|checkpoint state| PG

    MINIO -->|ObjectCreated notification| REDIS
    REDIS -->|consume events| WORKER

    WORKER -->|orchestrate ingest| IG
    IG -->|fetch file| MINIO
    IG -->|extract + embed| LLM
    IG -->|upsert vectors| QD
    IG -->|update status| PG
    IG -->|traces| LF
```

---

## Query Graph (Chat RAG)

Every chat query runs through a 7-node LangGraph pipeline:

```mermaid
flowchart LR
    A[classify_intent] --> B[rewrite_query]
    B --> C[retrieve]
    C --> D[grade_documents]
    D -->|insufficient, retry < 2| E[refine_query]
    E --> C
    D -->|no context found| F[answer_not_found]
    D -->|sufficient context| G[generate]
    G --> H[guardrails_output]
    F --> H
    H --> I[persist]
```

| Node | Responsibility |
|---|---|
| `classify_intent` | Topical question vs. small talk vs. out-of-scope |
| `rewrite_query` | Rewrite / decompose query for better retrieval |
| `retrieve` | Vector search via `RetrievalService` (tenant + collection filter, top_k=8) |
| `grade_documents` | LLM grades each chunk for relevance to the original question |
| `generate` | Answer with inline citations from graded chunks |
| `guardrails_output` | PII check, industry disclaimer, prompt injection defense |
| `persist` | Save conversation, citations, token counts, LangGraph checkpoint |

Retrieval enforces `tenant_id + allowed_collection_ids` from JWT — implemented once in `RetrievalService`, never in individual handlers.

### Chat Sequence

```mermaid
sequenceDiagram
    actor User
    participant OWUI as Open WebUI
    participant API as RAG API
    participant QG as Query Graph
    participant RS as RetrievalService
    participant QD as Qdrant
    participant LLM as Ollama / vLLM
    participant PG as PostgreSQL

    User->>OWUI: Send message
    OWUI->>API: POST /v1/chat/completions (stream: true)
    API->>API: Verify JWT, extract user_ctx
    API->>PG: Load allowed_collections for user roles
    API->>QG: Invoke query graph
    QG->>LLM: classify_intent
    QG->>LLM: rewrite_query
    QG->>RS: retrieve(rewritten_query, user_ctx)
    RS->>QD: Vector search (tenant + collection filter)
    QD-->>RS: Scored chunks
    QG->>LLM: grade_documents
    QG->>LLM: generate answer + citations
    QG->>QG: guardrails_output
    QG->>PG: persist (conversation, sources, checkpoint)
    QG-->>API: Final state
    API-->>OWUI: SSE stream
    OWUI-->>User: Answer + source documents
```

---

## Ingest Pipeline

Document ingest is fully asynchronous and event-driven:

```
User upload (POST /documents/upload)
      |
   MinIO PUT  (tenant-{slug}/raw/{collection}/{doc_id}/{filename})
      |
MinIO bucket notification --> Redis Streams "ingest_events"
      |
Ingest Worker (consumer group, dead-letter after 3 failures)
      |
  LangGraph Ingest Graph:
      fetch --> validate --> pii_scan --> dedupe --> extract --> chunk --> embed --> upsert --> persist
```

### Ingest Graph Nodes

| Node | Responsibility |
|---|---|
| `node_fetch` | Download file from MinIO |
| `node_validate` | MIME type, size, LLM document classification and quality check |
| `node_pii_scan` | Detect PII/sensitive data; tag or flag for admin review |
| `node_dedupe` | SHA-256 hash + semantic near-duplicate detection |
| `node_extract` | Text extraction (unstructured/docling), optional vision OCR |
| `node_chunk` | Recursive / semantic chunking per collection config (default: 512 tokens / 64 overlap) |
| `node_embed` | Generate embeddings via model from `models_registry` |
| `node_upsert` | Upsert to Qdrant with deterministic `point_id` (idempotent) |
| `node_persist` | Write final status + metadata to PostgreSQL |

**Document lifecycle:** `uploaded -> validating -> needs_review / indexing -> ready / rejected / failed`

### Ingest Sequence

```mermaid
sequenceDiagram
    actor User
    participant API as RAG API
    participant PG as PostgreSQL
    participant MINIO as MinIO
    participant REDIS as Redis Streams
    participant WORKER as Ingest Worker
    participant IG as Ingest Graph
    participant LLM as Ollama / vLLM
    participant QD as Qdrant

    User->>API: POST /documents/upload (multipart)
    API->>API: Validate MIME, size, permissions
    API->>PG: INSERT document (status=uploaded)
    API->>MINIO: PUT object
    API-->>User: 202 Accepted {document_id, job_id}
    MINIO->>REDIS: Publish ingest event
    REDIS->>WORKER: Consume event
    WORKER->>IG: Run ingest graph
    IG->>MINIO: Fetch file
    IG->>LLM: Validate, classify, PII scan
    IG->>IG: Dedupe check (SHA-256 + semantic)
    IG->>LLM: Extract text + embed
    IG->>QD: Upsert vectors (deterministic point_id)
    IG->>PG: Update status -> ready
```

---

## Repository Structure

```
src/
  api/            # FastAPI routers, Pydantic schemas, dependencies (auth, RBAC)
  core/           # config, logging, exceptions, clients (qdrant, minio, redis, llm)
  domain/         # business services (chat, tenants, audit, TOS, MAU)
  graphs/
    query_graph/  # 7-node RAG query graph (node_*.py files + state.py)
    ingest_graph/ # 9-node document ingest graph
    research_graph/ # multi-step research / deep-dive graph
  ingest/         # worker process, event processor, job tracker
  retrieval/      # RetrievalService -- only module with direct Qdrant access
  db/
    models/       # SQLAlchemy ORM models
    repositories/ # repository layer (no business logic)
    migrations/   # Alembic migrations (one logical change = one file, always has downgrade)
tests/
  unit/           # graph nodes, RBAC, chunking (LLM mocked via interface)
  integration/    # end-to-end via testcontainers (Postgres, Qdrant, MinIO, Redis)
  eval/           # RAGAS: 50 questions + 20 trap questions; faithfulness/relevance baseline
docs/             # PRD, Architecture (ADRs), API spec, Data model, Security/GDPR, Roadmap
```

**Layer rule:** `api -> domain -> core`. Importing from `api` into `domain` or `core` is forbidden (enforced by import-linter in CI).

---

## Data Model (key entities)

```
tenants ──< collections ──< documents ──< chunks_registry
   |              |               |
   |         chunk_config    document_versions
   |
   +──< user_tenant (user + tenant + role join)
   +──< rag_pipelines (pipeline config per model name)
   +──< conversations ──< messages ──< message_sources (citations)
   |                            +──< message_feedback
   +──< ingestion_jobs (per-document ingest status tracking)
   +──< audit_log
   +──< models_registry (LLM + embedding model catalogue)
   +──< tos_versions ──< tos_acceptances
```

---

## Security & Tenant Isolation

- `tenant_id` on every business table; every SQL query filtered from JWT context
- Qdrant queries always route through `RetrievalService` with automatic `tenant_id + collection_id` MUST filter
- JWT: signature verified via Keycloak JWKS; `exp`, `aud` validated; roles/tenant from token only
- Upload: MIME + size validation (100 MB), sanitized filename, presigned URL TTL <= 5 min
- Logs: no document content, prompts, responses, or PII; identifiers only; Langfuse PII masking enabled
- GDPR Art. 17: cascading deletion Postgres -> Qdrant -> MinIO via `DeletionService`
- Tenant isolation test suite is a CI merge blocker

---

## Local Development

### Prerequisites

Docker, Docker Compose, Python 3.12, `uv`

### Start the stack

```bash
docker compose up -d postgres redis minio qdrant keycloak rag-api openwebui
```

### Run migrations

```bash
DATABASE_URL=postgresql+asyncpg://raguser:ragpass@localhost:5432/ragdb \
PYTHONPATH=. python3 -m alembic upgrade head
```

### Run tests

```bash
pytest -x -q                              # unit tests
pytest tests/integration -m integration  # integration tests (testcontainers)
```

### Lint / format / types

```bash
ruff check --fix . && ruff format .
mypy src/
```

### Service URLs

| Service | URL | Credentials |
|---|---|---|
| Open WebUI | http://localhost:3000 | via Keycloak |
| RAG API docs | http://localhost:8000/docs | — |
| Keycloak admin | http://localhost:8080 | admin / admin |
| PostgreSQL | localhost:5432 | raguser / ragpass / ragdb |

---

## Roadmap

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. Foundations | Repo, CI, Docker Compose, Keycloak + JWT, FastAPI skeleton | `docker compose up` runs the full stack |
| 1. MVP | Full ingest pipeline, query graph, Open WebUI, conversation history, 2 models | Internal pilot: 20 docs, 5 users, 3 roles |
| 2. Production pilot | Admin review panel, audit log, Langfuse, hardening, DPIA, backup | Deployed at the clinic |
| 3. Product | Multi-tenant onboarding UI, K8s/Helm, OCR, hybrid retrieval, RAGAS eval | Second client (different industry) deployed from config, no code changes |

---

## Architecture Decision Records

Full ADRs are in [`docs/architecture.md`](docs/architecture.md). Key decisions:

- **Event-driven ingest** — upload decoupled from processing via MinIO -> Redis Streams -> Worker; worker failure does not block uploads
- **Single retrieval gateway** — `RetrievalService` is the sole Qdrant entry point, enforcing tenant isolation in one place
- **OpenAI-compatible abstraction** — all LLM/embedding calls behind a protocol interface; swapping a model = updating `models_registry`, no application code change
- **Postgres checkpointer** — LangGraph graph state checkpointed in Postgres, enabling conversation resume and ingest pipeline debugging
- **Idempotent ingest** — SHA-256 per tenant + deterministic Qdrant point IDs allow safe event replay without duplicating chunks
- **Stateless API** — all state in external stores; any API replica can be restarted or replaced without data loss

---

## AI-Assisted Development

The repository ships a versioned Claude Code setup used throughout development:

- [`CLAUDE.md`](CLAUDE.md) — project memory: architecture rules, layering and conventions
- [`.claude/agents/`](.claude/agents) — specialised sub-agents (architect, RAG engineer, backend, ML engineer, microservices, reviewer)
- [`.claude/skills/`](.claude/skills) — repeatable procedures: `/new-endpoint`, `/langgraph-node`, `/rag-eval`, `/tenant-isolation-check`
- [`tasks/`](tasks) — implementation backlog; each task maps to a feature branch and a reviewed PR

---

## License

Proprietary — © 2026 Bartosz Wolak. All rights reserved.
The source is published for review and evaluation only; see [`LICENSE`](LICENSE). For commercial licensing, contact [@bwolak94](https://github.com/bwolak94).
