# Reference Repository Analysis

**Project:** Universal RAG Platform · **Last updated:** 2026-07-13
**Purpose:** Architectural reference — what to borrow, what to avoid, and where this project differs from existing open-source RAG systems.

---

## TL;DR — Key decisions informed by this review

| Decision | Informed by |
|---|---|
| 4-role RBAC (Owner/Admin/Contributor/Viewer) | WeKnora — closest match to our model |
| Enforcement at retrieval layer, not endpoint | Onyx — document-level permission model |
| Collection-per-tenant in Qdrant (not shared + filter) | R2R + Qdrant multi-tenant best practices |
| Per-stage ingest status reporting | R2R `ingestion_jobs` pattern |
| Citation with source highlight | kotaemon — PDF highlight UX reference |
| Layout-aware chunking for medical PDFs | RAGFlow — deep document understanding |
| Langfuse as sole tracing backend | WeKnora — same stack decision |
| Open WebUI branding clause | Must keep "Open WebUI" visible for 50+ users or acquire enterprise license |
| LLM-based ingest validator | WeKnora — LLM classification at ingest |

---

## 1. Onyx (formerly Danswer)

**Repo:** [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx)
**Docs:** [docs.onyx.app/security/architecture/access_controls](https://docs.onyx.app/security/architecture/access_controls)

### What it does
Enterprise AI search and assistant. Hybrid RAG (keyword + semantic), knowledge graph, MCP agents. Connects to 40+ sources (Confluence, Slack, Google Drive, etc.) and preserves their native permissions.

### Permission model
Three roles: **Admin** (full system), **Curator** (content management — enterprise only), **User** (chat/search).

Document-level permissions, not just endpoint-level:
- Automated sync from source (Google Drive, Confluence) → preserves original ACLs.
- Manual assignment per user or group.
- Retrieval enforces permissions: users only get chunks from sources they are authorized to access — even if embeddings share a vector database.

**RBAC gating (Admin/Curator) is Enterprise Edition only.** Community edition has no role separation.

### Architecture patterns worth borrowing
- **Permission enforcement at the retrieval layer, not the router.** This is exactly our approach — `RetrievalService` applies the `tenant_id` + `allowed_collections` filter before any chunk is returned.
- **Document-level ACL decoupled from collection membership.** We should model `allowed_collections` per user/role in Postgres, not hardcode it in the JWT.

### What we do differently
- We use LangGraph for the query pipeline; Onyx has its own orchestration.
- We are multi-tenant from day one; Onyx's multi-tenant support is enterprise-only.
- We self-host all models (Ollama/vLLM); Onyx supports cloud LLMs as primary targets.

---

## 2. WeKnora (Tencent)

**Repo:** [Tencent/WeKnora](https://github.com/Tencent/WeKnora)
**Docs:** [deepwiki.com/Tencent/WeKnora](https://deepwiki.com/Tencent/WeKnora)

### What it does
Open-source LLM knowledge platform: RAG + autonomous reasoning agent + self-maintaining wiki. Production-grade multi-tenant architecture with RBAC.

### Permission model — closest to ours
**Four-tier role matrix (identical to our target model):**

| Role | Scope |
|---|---|
| **Owner** | Full control over knowledge bases and org resources |
| **Admin** | Administrative privileges + audit access |
| **Contributor** | Add/modify content within assigned knowledge bases |
| **Viewer** | Read-only access |

Additional features:
- Per-KB (collection) resource ownership, not just per-tenant.
- Per-tenant audit log with user action tracking.
- Invite-only workspaces; self-service tenant creation.
- Fine-grained ownership tracking per chunk, knowledge entry, and knowledge base.

### Security patterns worth borrowing
- **AES-256-GCM at-rest encryption for API keys** — relevant for our Keycloak client secrets stored in Postgres.
- **SSRF-safe HTTP client** — we must apply the same when proxying user-supplied URLs in connectors.
- **Sandbox isolation for agent skills** — relevant for our guardrails output node.
- **Langfuse as sole tracing backend** for ReAct loops, token tracking, and pipeline traces. Same decision we made; confirms the approach.

### Architecture patterns worth borrowing
- **LLM-based classification at ingest** — WeKnora classifies documents using an LLM during ingestion (same as our planned ingest validator). Target: ≥ 90% correct categories.
- **Dependency injection for service lifecycle** — clean repo-layer abstraction, not DI framework but the pattern applies to our `RetrievalService` / `DeletionService` separation.

### Vector isolation
WeKnora supports Qdrant among several vector DBs but does not prescribe a specific isolation strategy (collection-per-tenant vs payload filter). **Our decision: collection-per-tenant** (see ADR in `docs/architecture.md`).

---

## 3. R2R (SciPhi)

**Repo:** [SciPhi-AI/R2R](https://github.com/SciPhi-AI/R2R)

### What it does
Production RAG backend with RESTful API. Described as "Supabase for RAG." Over 1 million questions answered in production. Latest: v3.6.6 (Aug 2025).

### Permission model
Two roles: **Admin** (all routes) and **User** (user routes only). Role determined at login by probing `/system/settings` endpoint — simple but pragmatic.

**Collection-based document organization:**
- Collections are containers for documents with user-level access management.
- Granular actions: `create_group`, `update_group`, `add_user_to_group`, `add_document_to_group`.
- Query API accepts `selectedCollectionIds: string[]` — filters retrieval by collection membership.
- Collections are logical permission boundaries, not full multi-tenant isolation (shared infrastructure per deployment).

**Assessment:** R2R's permission model is simpler than ours (2 roles vs 4), and multi-tenancy is per-deployment rather than per-tenant-within-deployment. However, the **collection abstraction and granular group actions are a strong API design reference.**

### Architecture patterns worth borrowing
- **Per-stage ingest status reporting** → `ingestion_jobs` table with stage + status + metadata per step. We implement this identically.
- **S3-compatible file provider** (added 2025) as alternative to Postgres large objects — confirms MinIO as the right choice for our file storage.
- **User-defined agent tooling** — relevant for future extensibility of our query graph.

### What we do differently
- We have 4 RBAC roles; R2R has 2.
- We use LangGraph for graph orchestration; R2R uses its own pipeline.
- We enforce structural multi-tenancy (one deployment, many tenants); R2R assumes one tenant per deployment.

---

## 4. RAGFlow (Infiniflow)

**Repo:** [infiniflow/ragflow](https://github.com/infiniflow/ragflow)
**Docs:** [deepwiki.com/infiniflow/ragflow](https://deepwiki.com/infiniflow/ragflow)

### What it does
RAG engine with **deep document understanding** — the best open-source system for parsing complex documents (PDFs with tables, figures, complex layouts). One of the fastest-growing open-source projects (2,596% YoY contributor growth).

### Document parsing — most relevant feature
- **Layout-aware chunking**: uses vision models to identify document structure before chunking. Tables are extracted with high fidelity, not treated as flat text.
- **Supported parsers**: MinerU, Docling (added Oct 2025), native deep learning models.
- **Multimodal support**: uses a multimodal LLM to interpret images within PDF/DOCX files (added Mar 2025).
- **Format coverage**: PDF, DOCX, Excel, PPT, HTML, Markdown.

### Architecture
Multi-tier microservices. Separate async background processing from synchronous API operations — same pattern as our ingest worker.

### What to borrow
- **Docling as a document parsing backend** for our ingest pipeline's extraction stage. Medical documents (discharge summaries, lab reports, procedures) have complex table structures that recursive text splitting destroys. Docling preserves them.
- **Per-document-type chunking strategy** (already in our `collections.chunk_config`) — RAGFlow validates this as the right abstraction.
- **Vision model for image-heavy PDFs** — consider as an optional stage in the ingest graph for radiology reports or scanned forms.

### What we do differently
- RAGFlow has no multi-tenant RBAC; it is a single-tenant system.
- RAGFlow does not use LangGraph; it has its own pipeline orchestration.
- We keep parsing as one stage in the LangGraph ingest graph, not a separate service.

---

## 5. Cognita (TrueFoundry)

**Repo:** [truefoundry/cognita](https://github.com/truefoundry/cognita)

### What it does
Modular, production-ready RAG framework built on LangChain + LlamaIndex. Configurable via YAML or Python DSL. Supports Qdrant and SingleStore as vector DBs.

### Architecture patterns
- **Collection = set of documents from one or more data sources.** Clean abstraction, maps directly to our `collections` table.
- **LLM Gateway as central proxy** for all embedding and LLM calls — single unified OpenAI-compatible endpoint. This is exactly our `models_registry` + abstract client pattern.
- **YAML-driven pipeline configuration** — useful reference for how we might expose collection-level chunking config to admins without code changes.
- **Kubernetes + serverless deployment templates** — reference for our Phase 3 K8s manifests.

### What we do differently
- Cognita has no multi-tenant RBAC — it is a framework, not a platform.
- Cognita uses LangChain/LlamaIndex; we use LangGraph for explicit state machine control.
- We have event-driven ingest via Redis Streams; Cognita uses synchronous ingest.

---

## 6. kotaemon (Cinnamon)

**Repo:** [Cinnamon/kotaemon](https://github.com/Cinnamon/kotaemon)

### What it does
Self-hosted chat-with-documents application. Best-in-class citation UX: answers include source passage highlights directly in an in-browser PDF viewer.

### Citation UX — reference for our frontend
- **In-browser PDF viewer with highlighted passages** and relevance score display.
- Citations include: document title, page number, direct link, and the exact passage.
- Multi-user login with private/public collections and shareable chats.

### RAG pipeline
- Hybrid retriever: full-text (BM25) + vector search with re-ranking.
- Supports question decomposition for multi-hop reasoning.
- ReAct, ReWOO agent modes.

### What to borrow
- **Citation display format** for our Open WebUI integration: `message_sources` should include `{doc_id, chunk_id, page_number, highlight_text, score}` — enough for the frontend to render a highlighted PDF viewer.
- **Relevance score display** — expose score in `message_sources` so the admin can tune retrieval thresholds with evidence.

### What we do differently
- kotaemon has minimal RBAC (private/public only); no enterprise multi-tenancy.
- No event-driven ingest; no LangGraph.

---

## 7. Open WebUI

**Repo:** [open-webui/open-webui](https://github.com/open-webui/open-webui)

### Licensing — CRITICAL for production

Effective **April 19, 2025 (v0.6.6)**, Open WebUI introduced a branding protection clause:

> You **must keep "Open WebUI" branding visible** unless:
> - Your deployment serves **50 or fewer users** in any 30-day window, **OR**
> - You hold an **enterprise license** (white-label).

**Impact on this project:**
- Medical clinic pilot (likely < 50 users): no issue with community license.
- Scaling to multiple tenants / organisations: enterprise license required for white-label.
- Contact: sales@openwebui.com (requires official work email).

### Architecture integration
- Used as our chat frontend, configured as an OpenAI-compatible client pointing to our RAG API.
- Pipeline names should be business-friendly (e.g., "Medical Procedures", not "rag-proc-v2") — per our `design` agent guidelines.

**Action item:** Add licence threshold monitoring. When monthly active users approaches 50, trigger a review of whether enterprise licence is needed.

---

## 8. Dify

**Repo:** [langgenius/dify](https://github.com/langgenius/dify)

### Multi-tenancy licensing — WARNING

Dify's licence explicitly states:

> **"Unless explicitly authorized by Dify in writing, you may not use the Dify source code to operate a multi-tenant environment."**

One workspace = one tenant. **Do not use Dify as a component or reference for multi-tenant features without a commercial agreement.**

### What is useful
- **Configurable chunking pipeline** (chunk size, overlap, indexing strategy via UI) — reference for an admin configuration panel.
- **SSO integration patterns** (SAML, OIDC, OAuth2 via Keycloak) — Dify Enterprise's SSO design is a reference for our Keycloak integration.
- **Visual workflow builder** — not applicable to our LangGraph approach, but illustrates what non-technical admins expect.

---

## 9. AnythingLLM

**Repo:** Available on GitHub

### Assessment
Workspace-centric platform for easy document RAG. Simpler permission model (workspace-level, basic access control). Suitable for small teams, not enterprise multi-tenancy.

**Not a useful architectural reference for our project** — our requirements (structural tenant isolation, 4-role RBAC, event-driven ingest, audit log) significantly exceed what AnythingLLM addresses.

---

## 10. Architectural references (smaller repos)

### scalable-rag-pipeline (FareedKhan-dev)
[GitHub link](https://github.com/FareedKhan-dev/scalable-rag-pipeline)

LangGraph + Ray + Kubernetes. Key pattern:
- Each service (LangGraph API, PDF Ingestion Worker, Qdrant) as a separate Kubernetes Deployment.
- Terraform for EKS + Aurora Postgres Serverless v2.
- **Reference for our Phase 3 K8s manifests** (`microservices` agent, `docs/architecture.md §7`).

### Medium: Production RAG on Kubernetes with LangGraph + Qdrant
[Article](https://medium.com/@rithvikbng/deploying-a-production-ready-rag-on-kubernetes-multi-tenant-qdrant-streaming-pdf-ingestion-llm-82356f315f1b)

Specifically covers multi-tenant Qdrant + streaming PDF ingest + LangGraph on K8s — the closest public reference to our exact stack combination.

### flexible-graphrag (stevereiner)
[GitHub link](https://github.com/stevereiner/flexible-graphrag)

15 property graph DBs, 4 RDF stores, 10 vector DBs, Docling/LlamaParse, GraphRAG. Useful as a reference for **GraphRAG extension** if the project expands beyond pure vector RAG toward knowledge graphs.

### Milvus blog: Multi-Tenancy RAG best practices
[Article](https://milvus.io/blog/build-multi-tenancy-rag-with-milvus-best-practices-part-one.md)

Three isolation strategies for vector databases:
1. **Collection-per-tenant** — strongest isolation, higher resource cost. **Our choice for Qdrant.**
2. **Partition-per-tenant** — moderate isolation, shared index overhead.
3. **Payload filter per tenant** — lowest isolation, highest density risk (cross-tenant leakage on misconfiguration).

---

## Comparison matrix

| System | Multi-tenant | RBAC roles | Qdrant | LangGraph | Event-driven ingest | Audit log | GDPR / medical |
|---|---|---|---|---|---|---|---|
| **This project** | **Yes (structural)** | **4 (Owner/Admin/Contributor/Viewer)** | **Yes (collection-per-tenant)** | **Yes** | **Yes (Redis Streams)** | **Yes (per action)** | **Yes (Art.17, PII masking)** |
| Onyx | Enterprise only | 3 (Admin/Curator/User) | No | No | Connector sync | Partial | Partial |
| WeKnora | Yes | 4 (Owner/Admin/Contributor/Viewer) | Optional | No | No | Yes | Partial |
| R2R | Per-deployment | 2 (Admin/User) | Optional | No | No | No | No |
| RAGFlow | No | No | No | No | Async workers | No | No |
| Cognita | No | No | Yes | No | Sync | No | No |
| kotaemon | Minimal | 2 (private/public) | No | No | No | No | No |
| Dify | Licence-restricted | Workspace | No | No | Async | Enterprise | Partial |
| AnythingLLM | Workspace | Workspace | No | No | No | No | No |

**Finding:** No existing open-source system combines our full stack (LangGraph + MinIO events + LLM ingest validation + structural multi-tenancy + 4-role RBAC + GDPR Art.17 cascade). This project occupies a unique position between WeKnora (closest RBAC model) and RAGFlow (closest parsing depth).

---

## Action items for implementation

The following items are **not yet reflected in the codebase or documentation** and should be addressed as the project progresses:

1. **Docling integration** for ingest extraction stage — reference RAGFlow's parsing approach for medical PDFs with complex tables. Create ADR before implementation (`/adr`).
2. **`allowed_collections` per user/role in Postgres** — decouple from JWT claims, allow dynamic assignment. Reference Onyx's manual group assignment model.
3. **Citation format** — `message_sources` should include `{doc_id, chunk_id, page_number, highlight_text, score}`. Reference kotaemon's PDF highlight UX.
4. **Open WebUI licence tracking** — add monthly active user monitoring; trigger review at 40 users (10 below the 50-user branding threshold).
5. **AES-256-GCM for stored API keys** — reference WeKnora; apply to Keycloak client secrets in Postgres.
6. **SSRF protection** for any HTTP calls from user-supplied URLs (future connector feature) — reference WeKnora's SSRF-safe HTTP client.
7. **Phase 3 K8s manifests** — reference `scalable-rag-pipeline` for LangGraph + worker + Qdrant deployment topology.
