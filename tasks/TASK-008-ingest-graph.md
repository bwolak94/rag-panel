# TASK-008: LangGraph Ingest Graph

**Status:** TODO
**Priority:** P0 — required for any document processing
**Owner:** backend-dev + rag-engineer
**Reviewer:** python-reviewer (security profile) + security-auditor
**Related docs:** `docs/architecture.md` §5, §8, §15, §16 | `docs/data-model.md` §2.2 | `docs/rodo.md`
**Estimated effort:** 5–7 days

---

## Overview

Implement the LangGraph ingest graph in `src/graphs/ingest_graph/`. The graph processes documents uploaded to MinIO through a 9-node pipeline: fetch → extract → dedupe → validate → pii_scan → chunk → embed → upsert → persist. Each node is a separate file following the `node_<name>.py` convention. Graph state is a Pydantic model `IngestState` in `state.py`. The Postgres LangGraph checkpointer enables graph resumption after the `pii_scan` node pauses a document for admin review.

The ingest graph is invoked by the Ingest Worker (`src/ingest/worker.py`) after it consumes an event from the `ingest_events` Redis Stream. Each event carries a `document_id`, `tenant_id`, `collection_id`, and `minio_key` (see `docs/architecture.md` §15 for the canonical event schema).

This graph is the implementation target for the `docs/architecture.md` §8 design. Every node must update `ingestion_jobs.steps` before returning — this is the single audit trail for the pipeline and the basis for admin monitoring.

---

## Usage

The worker instantiates the compiled graph once at startup and reuses it across events:

```python
# src/ingest/worker.py
from src.graphs.ingest_graph.graph import build_ingest_graph

graph = build_ingest_graph()

async def handle_event(event: IngestEvent, db: AsyncSession) -> None:
    job = await create_ingestion_job(db, event)
    config = {"configurable": {"thread_id": str(job.langgraph_thread_id)}}
    initial_state = IngestState(
        document_id=event.document_id,
        tenant_id=event.tenant_id,
        collection_id=event.collection_id,
        minio_key=event.minio_key,
        job_id=job.id,
    )
    result = await graph.ainvoke(initial_state.model_dump(), config=config)
```

Graph resumption after admin approval (see `docs/architecture.md` §6):

```python
# src/api/routers/documents.py — POST /documents/{id}/review
thread_id = job.langgraph_thread_id
config = {"configurable": {"thread_id": str(thread_id)}}
# Resume from the last checkpoint at the chunk node
await graph.aupdate_state(config, {"status": "indexing"}, as_node="pii_scan")
result = await graph.ainvoke(None, config=config)
```

---

## Tech Stack

- **LangGraph** `0.2+` — graph compilation, node routing, Postgres checkpointer
- **langgraph-checkpoint-postgres** — `AsyncPostgresSaver` for checkpoint persistence
- **Docling** — layout-aware PDF/DOCX parsing (extract node)
- **LangChain text splitters** — `RecursiveCharacterTextSplitter`, `MarkdownTextSplitter` (chunk node)
- **tiktoken** — token counting for chunk validation
- **httpx** — async HTTP client for OpenAI-compatible embedding API calls
- **tenacity** — retry logic with exponential backoff per `docs/architecture.md` §16
- **minio-py** — MinIO object download in fetch node
- **spacy** or **presidio-analyzer** — NER-based PII detection in pii_scan node
- **hashlib** — SHA-256 verification and deterministic point_id generation
- **structlog** — structured logging (no content, no PII)

---

## Database Patterns

All repository calls go through `src/db/repositories/`. No raw SQL in node files.

### Tables used

**`ingestion_jobs`** — Every node writes a step entry before and after execution. Schema for `steps` array elements (from `docs/data-model.md` §2.2):

```python
class IngestStep(BaseModel):
    stage: str          # node name: fetch | extract | dedupe | validate | pii_scan | chunk | embed | upsert | persist
    status: str         # running | completed | failed | skipped
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    meta: dict = {}     # stage-specific counters (never content)
```

Step meta conventions per node (never log actual content):

| Node | Allowed meta keys |
|---|---|
| fetch | `size_bytes`, `latency_ms` |
| extract | `page_count`, `word_count`, `section_count`, `latency_ms` |
| dedupe | `result` (`unique` or `duplicate`), `existing_document_id` |
| validate | `category`, `confidence`, `quality_score`, `document_type`, `latency_ms` |
| pii_scan | `pii_detected` (bool), `flags_count` (int), `latency_ms` — NEVER flag values or content |
| chunk | `strategy`, `chunk_count`, `avg_token_count`, `latency_ms` |
| embed | `model_id`, `batch_count`, `total_vectors`, `latency_ms` |
| upsert | `points_upserted`, `latency_ms` |
| persist | `chunks_registry_rows`, `latency_ms` |

**`documents`** — Updated at `validate` (writes `validation_result` JSONB), `pii_scan` (may update `status` to `needs_review`), and `persist` (writes `status=ready`). Always verify `document.tenant_id == state.tenant_id` before writing.

**`chunks_registry`** — Bulk insert in `persist` node. Each row: `(tenant_id, document_id, qdrant_point_id, chunk_index, page, section, token_count)`.

**`collections`** — Read in `chunk` node (for `chunk_config`) and `embed` node (for `embedding_model_id`). Never mutated by the graph.

**`models_registry`** — Read in `embed` node to resolve endpoint URL and model identifier. Cached in graph state after first read.

---

## Architecture — SOLID & DRY

### File layout

```
src/graphs/ingest_graph/
    __init__.py
    graph.py            # build_ingest_graph() factory — compile and wire nodes
    state.py            # IngestState Pydantic model
    nodes/
        __init__.py
        node_fetch.py
        node_extract.py
        node_dedupe.py
        node_validate.py
        node_pii_scan.py
        node_chunk.py
        node_embed.py
        node_upsert.py
        node_persist.py
    routing.py          # Conditional edge functions (route_after_dedupe, route_after_pii)
    prompts/            # Symlink or reference to src/graphs/prompts/
```

### IngestState model (`state.py`)

```python
from __future__ import annotations
import enum
from uuid import UUID
from datetime import datetime
from typing import Annotated
from pydantic import BaseModel, Field
from langgraph.graph import add_messages  # not used here but imported for pattern

class DocumentStatus(str, enum.Enum):
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    NEEDS_REVIEW = "needs_review"
    INDEXING = "indexing"
    READY = "ready"
    REJECTED = "rejected"
    FAILED = "failed"

class Section(BaseModel):
    heading: str | None
    text: str
    page: int | None
    section_index: int

class ValidationResult(BaseModel):
    category: str | None = None
    confidence: float | None = None
    quality_score: float | None = None
    document_type: str | None = None
    language: str | None = None
    pii_flags: list[str] = Field(default_factory=list)  # type labels only, not values
    reasons: list[str] = Field(default_factory=list)

class ChunkData(BaseModel):
    chunk_index: int
    text: str
    page: int | None
    section: str | None
    token_count: int
    point_id: UUID                     # sha256-derived, deterministic

class IngestState(BaseModel):
    # Identity (set by worker, never mutated)
    document_id: UUID
    tenant_id: UUID
    collection_id: UUID
    minio_key: str
    job_id: UUID
    sha256: str = ""                   # populated by fetch node

    # Intermediate artifacts
    raw_bytes: bytes | None = None
    extracted_text: str | None = None
    extracted_sections: list[Section] | None = None
    validation_result: ValidationResult | None = None
    chunks: list[ChunkData] | None = None
    embeddings: list[list[float]] | None = None
    point_ids: list[UUID] | None = None

    # Pipeline control
    status: str = "uploaded"
    error: str | None = None
    retry_count: int = 0
    halt: bool = False                 # set True to stop graph (duplicate / needs_review)
```

### Node contract (all nodes follow this pattern)

```python
# Example: node_fetch.py
from src.graphs.ingest_graph.state import IngestState
from src.core.clients.minio import get_minio_client

async def node_fetch(state: IngestState) -> dict:
    """Download document from MinIO; validate sha256 integrity.

    Args:
        state: Current ingest graph state.

    Returns:
        Dict of state fields mutated by this node.
    """
    # ... implementation
    return {
        "raw_bytes": content,
        "sha256": computed_hash,
    }
```

Nodes return only the fields they mutate — LangGraph merges partial updates into state automatically.

### Conditional routing (`routing.py`)

```python
def route_after_dedupe(state: IngestState) -> str:
    if state.halt:
        return END
    return "node_validate"

def route_after_validate(state: IngestState) -> str:
    """Route to END when validate sets needs_review/halt, otherwise continue to pii_scan."""
    if state.status == "needs_review" or state.halt:
        return END
    return "node_pii_scan"

def route_after_pii(state: IngestState) -> str:
    if state.status == "needs_review":
        return END   # graph pauses; checkpoint saved
    return "node_chunk"
```

### Graph compilation (`graph.py`)

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from src.graphs.ingest_graph.state import IngestState
from src.graphs.ingest_graph import nodes
from src.graphs.ingest_graph.routing import (
    route_after_dedupe,
    route_after_validate,
    route_after_pii,
)

def build_ingest_graph(checkpointer: AsyncPostgresSaver | None = None) -> ...:
    builder = StateGraph(IngestState)

    builder.add_node("node_fetch",    nodes.node_fetch)
    builder.add_node("node_extract",  nodes.node_extract)
    builder.add_node("node_dedupe",   nodes.node_dedupe)
    builder.add_node("node_validate", nodes.node_validate)
    builder.add_node("node_pii_scan", nodes.node_pii_scan)
    builder.add_node("node_chunk",    nodes.node_chunk)
    builder.add_node("node_embed",    nodes.node_embed)
    builder.add_node("node_upsert",   nodes.node_upsert)
    builder.add_node("node_persist",  nodes.node_persist)

    builder.set_entry_point("node_fetch")
    builder.add_edge("node_fetch",    "node_extract")
    builder.add_edge("node_extract",  "node_dedupe")
    # Conditional: halt on duplicate → END, otherwise → node_validate
    builder.add_conditional_edges("node_dedupe", route_after_dedupe, {
        "node_validate": "node_validate",
        END: END,
    })
    # Conditional: halt on low quality / needs_review → END, otherwise → node_pii_scan
    builder.add_conditional_edges("node_validate", route_after_validate, {
        "node_pii_scan": "node_pii_scan",
        END: END,
    })
    builder.add_conditional_edges("node_pii_scan", route_after_pii, {
        "node_chunk": "node_chunk",
        END: END,
    })
    builder.add_edge("node_chunk",    "node_embed")
    builder.add_edge("node_embed",    "node_upsert")
    builder.add_edge("node_upsert",   "node_persist")
    builder.add_edge("node_persist",  END)

    return builder.compile(checkpointer=checkpointer)
```

---

## Implementation Steps

### Step 1: State and scaffolding

1. Create `src/graphs/ingest_graph/` directory structure.
2. Implement `IngestState` in `state.py` using Pydantic `BaseModel`. All fields optional except the four identity fields (`document_id`, `tenant_id`, `collection_id`, `minio_key`).
3. Create `routing.py` with `route_after_dedupe` and `route_after_pii` functions.
4. Create `graph.py` with `build_ingest_graph(checkpointer)` factory. Do not wire real nodes yet — use placeholder lambdas for first compile test.

### Step 2: node_fetch

File: `src/graphs/ingest_graph/nodes/node_fetch.py`

```python
import hashlib
from uuid import UUID
from src.core.clients.minio import MinIOClient
from src.core.exceptions import IngestNodeError
from src.graphs.ingest_graph.state import IngestState
from src.db.repositories.ingestion_jobs import update_step

async def node_fetch(state: IngestState, *, minio: MinIOClient, db) -> dict:
    step_start = utcnow()
    try:
        # Always read minio_key and minio_bucket from the document record —
        # never reconstruct the key from tenant_slug/collection_id/filename.
        # minio_key was stored verbatim at upload time (TASK-006: f"raw/{collection_id}/{document_id}/{filename}").
        document = await db.scalar(
            select(Document).where(
                Document.id == state.document_id,
                Document.tenant_id == state.tenant_id,
            )
        )
        if document is None:
            raise IngestNodeError(f"Document {state.document_id} not found for tenant {state.tenant_id}")

        # minio_bucket is stored on the document; fall back to f"tenant-{tenant_id}" if absent.
        bucket = document.minio_bucket or f"tenant-{state.tenant_id}"
        raw_bytes = await minio.get_object(
            bucket=bucket,
            key=document.minio_key,
        )
        computed_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        # Verify integrity against documents.sha256 stored at upload time
        if computed_sha256 != document.sha256:
            raise IngestNodeError(
                f"SHA-256 mismatch for document {state.document_id}: "
                f"expected={document.sha256[:8]}…, got={computed_sha256[:8]}…"
            )

        await update_step(db, state.job_id, stage="fetch", status="completed",
                          started_at=step_start, meta={"size_bytes": len(raw_bytes)})
        return {"raw_bytes": raw_bytes, "sha256": computed_sha256}

    except Exception as exc:
        await update_step(db, state.job_id, stage="fetch", status="failed",
                          started_at=step_start, error=str(exc))
        raise
```

Note: `minio` and `db` are injected via LangGraph's dependency injection via `config["configurable"]` or a closure in the compiled graph. The worker creates one session per event invocation.

### Step 3: node_extract

File: `src/graphs/ingest_graph/nodes/node_extract.py`

Use Docling for layout-aware parsing. Docling is run synchronously in an `asyncio.get_event_loop().run_in_executor(None, ...)` to avoid blocking the event loop.

```python
from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
import asyncio, io

async def node_extract(state: IngestState, *, db, minio: MinIOClient) -> dict:
    """Extract text and sections from raw document bytes.

    Args:
        state: Current ingest graph state.
        db: Async database session.
        minio: MinIO client injected via functools.partial or LangGraph RunnableConfig.

    Returns:
        Dict with ``extracted_text`` and ``extracted_sections`` fields.
    """
    step_start = utcnow()
    raw = state.raw_bytes
    mime = await get_document_mime(db, state.document_id)

    def _run_docling() -> tuple[str, list[Section]]:
        converter = DocumentConverter()
        # Docling accepts bytes via BytesIO
        source = io.BytesIO(raw)
        result = converter.convert(source, input_format=_mime_to_docling_format(mime))
        doc = result.document

        # Extract full text
        full_text = doc.export_to_markdown()

        # Extract sections with page numbers
        sections = [
            Section(
                heading=item.get("heading"),
                text=item["text"],
                page=item.get("page"),
                section_index=idx,
            )
            for idx, item in enumerate(doc.iter_items())
            if item.get("text")
        ]
        return full_text, sections

    # Use get_running_loop() — never get_event_loop() inside an async function
    loop = asyncio.get_running_loop()
    full_text, sections = await loop.run_in_executor(None, _run_docling)

    # Persist extracted.json to MinIO processed/ prefix for debugging/reprocessing.
    # Bucket derived from state.tenant_id — never from tenant_slug.
    document = await db.scalar(
        select(Document).where(Document.id == state.document_id)
    )
    bucket = (document.minio_bucket if document else None) or f"tenant-{state.tenant_id}"
    await minio.put_object(
        bucket=bucket,
        key=f"processed/{state.document_id}/extracted.json",
        data=_sections_to_json(sections),
    )

    word_count = len(full_text.split())
    await update_step(db, state.job_id, stage="extract", status="completed",
                      started_at=step_start,
                      meta={"page_count": len({s.page for s in sections if s.page}),
                            "word_count": word_count,
                            "section_count": len(sections)})
    return {"extracted_text": full_text, "extracted_sections": sections}
```

For TXT and MD files: skip Docling; split by newlines into sections directly.

Fallback for unsupported MIME types: raise `IngestNodeError("unsupported_mime_type")` — node marks document as `rejected`.

### Step 4: node_dedupe

File: `src/graphs/ingest_graph/nodes/node_dedupe.py`

```python
async def node_dedupe(state: IngestState, *, db) -> dict:
    step_start = utcnow()
    # Check (tenant_id, sha256) unique constraint per docs/data-model.md §2.2
    existing = await db.scalar(
        select(Document.id).where(
            Document.tenant_id == state.tenant_id,
            Document.sha256 == state.sha256,
            Document.id != state.document_id,      # exclude self
            Document.status != "deleted",
        )
    )
    if existing:
        await db.execute(
            update(Document)
            .where(Document.id == state.document_id)
            .values(status="rejected")
        )
        await update_step(db, state.job_id, stage="dedupe", status="completed",
                          started_at=step_start,
                          meta={"result": "duplicate",
                                "existing_document_id": str(existing)})
        return {"status": "rejected", "halt": True}

    await update_step(db, state.job_id, stage="dedupe", status="completed",
                      started_at=step_start, meta={"result": "unique"})
    return {}
```

### Step 5: node_validate

File: `src/graphs/ingest_graph/nodes/node_validate.py`

Calls the LLM via the OpenAI-compatible client. Prompt loaded from `src/graphs/prompts/validate_document_v1.md`.

```python
from src.core.clients.llm import LLMClient
from src.graphs.ingest_graph.state import IngestState, ValidationResult

# Prompt is only the template — actual document text is passed as a variable, never hardcoded.
# The prompt file path: src/graphs/prompts/validate_document_v1.md
PROMPT_NAME = "validate_document"
PROMPT_VERSION = 1

async def node_validate(state: IngestState, *, db, llm: LLMClient) -> dict:
    step_start = utcnow()
    collection = await get_collection(db, state.collection_id)
    validation_config = collection.validation_config
    confidence_threshold = validation_config.get("confidence_threshold", 0.7)

    # Sample first 2000 words to avoid excessive token usage
    sample_text = " ".join((state.extracted_text or "").split()[:2000])

    prompt = load_prompt(PROMPT_NAME, PROMPT_VERSION)
    response = await llm.chat_completion(
        model=await get_llm_model_id(db, collection.tenant_id),
        messages=[
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user.format(text=sample_text)},
        ],
        response_format={"type": "json_object"},
    )

    parsed = ValidationResult.model_validate_json(response.choices[0].message.content)

    # Persist to documents.validation_result
    await db.execute(
        update(Document)
        .where(Document.id == state.document_id,
               Document.tenant_id == state.tenant_id)  # resource-level auth
        .values(
            validation_result=parsed.model_dump(),
            category=parsed.category,
            language=parsed.language,
            status="validating",
        )
    )

    # Quality gate
    if parsed.quality_score is not None and parsed.quality_score < 0.3:
        # Mark as needs_review — too low quality to auto-index
        await db.execute(
            update(Document)
            .where(Document.id == state.document_id)
            .values(status="needs_review")
        )
        await update_step(db, state.job_id, stage="validate", status="completed",
                          started_at=step_start,
                          meta={"category": parsed.category,
                                "confidence": parsed.confidence,
                                "quality_score": parsed.quality_score})
        return {"validation_result": parsed, "status": "needs_review", "halt": True}

    await update_step(db, state.job_id, stage="validate", status="completed",
                      started_at=step_start,
                      meta={"category": parsed.category,
                            "confidence": parsed.confidence,
                            "quality_score": parsed.quality_score,
                            "document_type": parsed.document_type})
    return {"validation_result": parsed}
```

Create prompt file `src/graphs/prompts/validate_document_v1.md` with classification instructions. Never inline the prompt in the node file.

### Step 6: node_pii_scan

File: `src/graphs/ingest_graph/nodes/node_pii_scan.py`

This node uses a pluggable scanner interface so tests can inject deterministic mocks (as required by `TASK-016`):

```python
import asyncio
import re
from typing import ClassVar, Protocol

class PIIScanner(Protocol):
    async def scan(self, text: str) -> tuple[bool, list[str]]:
        """Returns (pii_detected, pii_type_labels_only)."""
        ...

class DefaultPIIScanner:
    """Regex + spacy NER pipeline."""
    _PATTERNS: ClassVar[list[str]] = [
        r"\b\d{11}\b",              # PESEL
        r"\b\d{3}-\d{3}-\d{3}\b",  # phone
        r"\b[A-Z]{2}\d{7}\b",       # passport
        r"\b\d{2}-\d{3}\b",         # postal code PL
    ]
    _COMPILED_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(p) for p in _PATTERNS
    ]

    async def scan(self, text: str) -> tuple[bool, list[str]]:
        # Run regex patterns
        flags: list[str] = []
        for pattern in self._COMPILED_PATTERNS:
            if pattern.search(text):
                flags.append(pattern.pattern)
        # Run spacy NER for PERSON, LOCATION entities
        # Use get_running_loop() — never get_event_loop() inside an async function
        doc = await asyncio.get_running_loop().run_in_executor(None, self._nlp, text[:10000])
        for ent in doc.ents:
            if ent.label_ in ("PERSON", "PESEL", "IDCARD"):
                flags.append(ent.label_)
        return bool(flags), list(set(flags))

async def node_pii_scan(
    state: IngestState,
    *,
    db,
    scanner: PIIScanner = DefaultPIIScanner(),
) -> dict:
    step_start = utcnow()
    text = state.extracted_text or ""
    pii_detected, pii_type_labels = await scanner.scan(text)

    collection = await get_collection(db, state.collection_id)
    pii_action = collection.validation_config.get("pii_action", "review")

    # Log ONLY counts and type labels — never actual PII values
    await update_step(db, state.job_id, stage="pii_scan", status="completed",
                      started_at=step_start,
                      meta={"pii_detected": pii_detected,
                            "flags_count": len(pii_type_labels)})

    if pii_detected and pii_action == "review":
        # Update validation_result.pii_flags (labels only)
        existing_vr = state.validation_result or ValidationResult()
        updated_vr = existing_vr.model_copy(update={"pii_flags": pii_type_labels})

        await db.execute(
            update(Document)
            .where(Document.id == state.document_id,
                   Document.tenant_id == state.tenant_id)
            .values(status="needs_review",
                    validation_result=updated_vr.model_dump())
        )
        await db.execute(
            update(IngestionJob)
            .where(IngestionJob.id == state.job_id)
            .values(status="awaiting_review")
        )
        return {"validation_result": updated_vr, "status": "needs_review"}

    return {}
```

### Step 7: node_chunk

File: `src/graphs/ingest_graph/nodes/node_chunk.py`

```python
import hashlib, uuid
from langchain.text_splitter import RecursiveCharacterTextSplitter, MarkdownTextSplitter
import tiktoken

async def node_chunk(state: IngestState, *, db) -> dict:
    step_start = utcnow()
    collection = await get_collection(db, state.collection_id)
    chunk_config = collection.chunk_config
    strategy = chunk_config.get("strategy", "recursive")
    chunk_size = chunk_config.get("chunk_size", 512)
    overlap = chunk_config.get("overlap", 64)

    # Per-document-type override
    doc_type = state.validation_result.document_type if state.validation_result else None
    overrides = chunk_config.get("document_type_overrides", {})
    if doc_type and doc_type in overrides:
        chunk_size = overrides[doc_type].get("chunk_size", chunk_size)
        overlap = overrides[doc_type].get("overlap", overlap)

    if strategy == "by_section" and state.extracted_sections:
        raw_chunks = _chunk_by_section(state.extracted_sections, chunk_size, overlap)
    elif strategy == "semantic":
        raw_chunks = _semantic_chunk(state.extracted_text, chunk_size, overlap)
    else:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=overlap,
            length_function=_token_count,
        )
        raw_chunks = splitter.split_text(state.extracted_text or "")

    enc = tiktoken.get_encoding("cl100k_base")
    chunks: list[ChunkData] = []
    for idx, (text, page, section) in enumerate(raw_chunks):
        # Deterministic point_id: SHA-256 is the canonical algorithm per rag-conventions.md.
        # UUID5 (SHA-1) is NOT used. Take the first 32 hex chars of the SHA-256 digest
        # to form a valid UUID.
        point_id = uuid.UUID(
            hashlib.sha256(f"{state.document_id}:{idx}".encode()).hexdigest()[:32]
        )
        chunks.append(ChunkData(
            chunk_index=idx,
            text=text,
            page=page,
            section=section,
            token_count=len(enc.encode(text)),
            point_id=point_id,
        ))

    await update_step(db, state.job_id, stage="chunk", status="completed",
                      started_at=step_start,
                      meta={"strategy": strategy,
                            "chunk_count": len(chunks),
                            "avg_token_count": sum(c.token_count for c in chunks) // max(len(chunks), 1)})
    return {"chunks": chunks}
```

### Step 8: node_embed

File: `src/graphs/ingest_graph/nodes/node_embed.py`

```python
async def node_embed(state: IngestState, *, db, llm: LLMClient) -> dict:
    step_start = utcnow()
    collection = await get_collection(db, state.collection_id)
    model_record = await get_model(db, collection.embedding_model_id)

    chunks = state.chunks or []
    texts = [c.text for c in chunks]

    # Batch in groups of 32 to respect vLLM/Ollama limits
    BATCH_SIZE = 32
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        response = await llm.embeddings(
            model=model_record.model_id,
            input=batch,
            base_url=model_record.endpoint_url,
        )
        all_embeddings.extend([item.embedding for item in response.data])

    await update_step(db, state.job_id, stage="embed", status="completed",
                      started_at=step_start,
                      meta={"model_id": str(model_record.id),
                            "batch_count": (len(texts) + BATCH_SIZE - 1) // BATCH_SIZE,
                            "total_vectors": len(all_embeddings)})
    return {"embeddings": all_embeddings}
```

### Step 9: node_upsert

File: `src/graphs/ingest_graph/nodes/node_upsert.py`

Only `RetrievalService.upsert_batch()` touches Qdrant. This node calls the service — it does NOT import `qdrant_client` directly.

Qdrant collection naming convention: `emb_{embedding_model_slug}` (e.g. `emb_bge-m3`). The collection name is resolved inside `RetrievalService` from the collection's `embedding_model_id`; `node_upsert` does not hardcode it. Do NOT use the legacy `chunks__{model_slug}` pattern.

```python
from src.retrieval.service import RetrievalService
from src.retrieval.schemas import QdrantPoint, TenantContext

async def node_upsert(state: IngestState, *, db, retrieval: RetrievalService) -> dict:
    step_start = utcnow()
    chunks = state.chunks or []
    embeddings = state.embeddings or []

    ctx = TenantContext(tenant_id=state.tenant_id)
    points: list[QdrantPoint] = []
    for chunk, vector in zip(chunks, embeddings):
        points.append(QdrantPoint(
            id=chunk.point_id,
            vector=vector,
            payload={
                "tenant_id": str(state.tenant_id),
                "collection_id": str(state.collection_id),
                "document_id": str(state.document_id),
                "chunk_index": chunk.chunk_index,
                "page": chunk.page,
                "section": chunk.section,
                "text": chunk.text,       # stored in payload, not used for filtering
                "created_at": int(utcnow().timestamp()),
            },
        ))

    await retrieval.upsert_batch(ctx, points)

    point_ids = [p.id for p in points]
    await update_step(db, state.job_id, stage="upsert", status="completed",
                      started_at=step_start,
                      meta={"points_upserted": len(points)})
    return {"point_ids": point_ids}
```

### Step 10: node_persist

File: `src/graphs/ingest_graph/nodes/node_persist.py`

```python
async def node_persist(state: IngestState, *, db) -> dict:
    step_start = utcnow()
    chunks = state.chunks or []
    point_ids = state.point_ids or []

    # Bulk insert chunks_registry (idempotent via ON CONFLICT DO NOTHING on qdrant_point_id)
    rows = [
        ChunksRegistry(
            tenant_id=state.tenant_id,
            document_id=state.document_id,
            qdrant_point_id=point_id,
            chunk_index=chunk.chunk_index,
            page=chunk.page,
            section=chunk.section,
            token_count=chunk.token_count,
        )
        for chunk, point_id in zip(chunks, point_ids)
    ]
    await db.execute(
        insert(ChunksRegistry)
        .values([r.__dict__ for r in rows])
        .on_conflict_do_nothing(index_elements=["qdrant_point_id"])
    )

    # Update document status to ready
    await db.execute(
        update(Document)
        .where(Document.id == state.document_id,
               Document.tenant_id == state.tenant_id)
        .values(status="ready")
    )

    # Update ingestion_job to completed
    await db.execute(
        update(IngestionJob)
        .where(IngestionJob.id == state.job_id)
        .values(status="completed", completed_at=utcnow())
    )

    # Audit log entry
    await insert_audit_log(db, AuditEntry(
        tenant_id=state.tenant_id,
        user_id=None,                  # system action
        action="document.indexed",
        resource_type="document",
        resource_id=state.document_id,
        details={"chunks_count": len(chunks), "job_id": str(state.job_id)},
    ))

    await update_step(db, state.job_id, stage="persist", status="completed",
                      started_at=step_start,
                      meta={"chunks_registry_rows": len(rows)})
    return {"status": "ready"}
```

### Step 11: Retry wrapper

Each node that makes external calls (fetch, validate, embed, upsert) is wrapped with tenacity per `docs/architecture.md` §16:

```python
from tenacity import retry, stop_after_attempt, wait_exponential, add_jitter

def _ingest_retry(func):
    """Applies the standard ingest node retry policy (3 attempts, exp backoff 2-30s)."""
    return retry(
        stop=stop_after_attempt(3),
        wait=add_jitter(wait_exponential(multiplier=2, min=2, max=30), 0.2),
        reraise=True,
    )(func)
```

---

## API Contracts

The ingest graph does not expose HTTP endpoints directly. It is invoked by the Ingest Worker. The following documents the boundaries:

### Input event schema (from `docs/architecture.md` §15)

```python
class IngestEvent(BaseModel):
    schema_version: Literal["1"]
    event_type: Literal["document.uploaded"]
    tenant_id: UUID
    document_id: UUID
    collection_id: UUID
    minio_bucket: str
    minio_key: str
    size_bytes: int
    content_type: str
    published_at: datetime
```

### Resume API (called by `POST /documents/{id}/review` in `TASK-011`)

After admin approval, the router resumes the graph at `node_chunk`. The graph checkpoint is identified by `ingestion_jobs.langgraph_thread_id`.

```python
# Thread ID must exist in langgraph_checkpoints
config = {"configurable": {"thread_id": str(job.langgraph_thread_id)}}
await graph.ainvoke({"status": "indexing"}, config=config)
```

---

## Security Checklist

- [ ] Every node that reads/writes a document verifies `document.tenant_id == state.tenant_id` before the operation.
- [ ] `node_pii_scan` logs ONLY `pii_detected: bool` and `flags_count: int` — never flag values, matched strings, or PII content.
- [ ] `validation_result.pii_flags` contains only type labels (`PERSON`, `PESEL`, etc.) — never extracted values.
- [ ] `node_extract` saves `extracted.json` to MinIO `processed/` prefix — this path must NOT be world-readable; presigned URL TTL for this path is 0 (no presigned URL generation).
- [ ] `node_upsert` imports only `RetrievalService` — no direct `qdrant_client` import. Verified by `tests/security/test_architecture.py` import linter.
- [ ] MinIO download uses internal network path, not presigned URL (worker has service account credentials).
- [ ] All exceptions caught at node level must re-raise as `IngestNodeError` — bare `except Exception` is forbidden.
- [ ] `raw_bytes` must not appear in any log statement. Use `len(raw_bytes)` for size logging.
- [ ] Docling is run in a thread executor — never block the asyncio event loop for CPU-bound extraction.
- [ ] The sha256 check in `node_fetch` is mandatory — a mismatch indicates storage corruption or tampering.

---

## Terms of Use (relevant constraints)

- **Topology is fixed**: node order `fetch → extract → dedupe → validate → pii_scan → chunk → embed → upsert → persist` is defined in `docs/architecture.md` §8. Any change requires ADR update.
- **No inline prompts**: `node_validate` loads its classification prompt from `src/graphs/prompts/validate_document_v1.md`. Never embed prompt text in Python code.
- **Idempotency**: `node_chunk` generates deterministic `point_id` using SHA-256: `uuid.UUID(hashlib.sha256(f"{doc_id}:{chunk_index}".encode()).hexdigest()[:32])`. SHA-256 is the canonical algorithm per `rag-conventions.md`; UUID5 (SHA-1) is NOT used. Reprocessing the same document does NOT duplicate Qdrant points.
- **Single Qdrant access path**: `node_upsert` calls `RetrievalService.upsert_batch()`. Any attempt to instantiate `qdrant_client.QdrantClient` outside `src/retrieval/` fails CI.
- **Checkpointer**: Use `AsyncPostgresSaver` from `langgraph-checkpoint-postgres`. Do NOT use the in-memory checkpointer in production.
- **PII content never leaves Postgres**: `validation_result` JSONB with PII flag labels is stored in Postgres and must not be forwarded to Langfuse, Redis, or any other store.

---

## Tests

Test files follow the structure from `TASK-016`. All nodes are tested in isolation with mocked dependencies.

### Unit tests per node (`tests/unit/ingest_graph/`)

**`test_node_fetch.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_fetch_success` | MinIO mock returns bytes; sha256 matches document record | Returns `raw_bytes`, `sha256`; step marked `completed` |
| `test_fetch_sha256_mismatch` | MinIO returns bytes with different sha256 | Raises `IngestNodeError`; step marked `failed` |
| `test_fetch_minio_unavailable` | MinIO mock raises connection error | Raises after 3 retries; step marked `failed` |

**`test_node_extract.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_extract_pdf` | PDF bytes (real test fixture in `tests/eval/fixtures/corpus/`) | Returns non-empty `extracted_text`, list of `Section`; step `completed` |
| `test_extract_txt_fallback` | TXT bytes, mime=text/plain | Returns text split into sections; no Docling call |
| `test_extract_unsupported_mime` | mime=application/exe | Raises `IngestNodeError("unsupported_mime_type")` |

**`test_node_dedupe.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_unique_document` | No other document with same sha256+tenant in DB | Returns `{}`, no status change |
| `test_duplicate_detected` | Existing document with same sha256+tenant_id | Returns `{"status": "rejected", "halt": True}`; document status set to `rejected` |
| `test_no_cross_tenant_dedupe` | Same sha256, different tenant_id | Not treated as duplicate — returns `{}` |

**`test_node_validate.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_valid_document` | LLM mock returns `{category: "procedure", confidence: 0.9, quality_score: 0.8}` | Returns `ValidationResult`; document updated; step `completed` |
| `test_low_quality_halts` | LLM returns `quality_score: 0.2` | Returns `{"status": "needs_review", "halt": True}` |
| `test_llm_unavailable_retries` | LLM mock raises on first 2 calls, succeeds on 3rd | Succeeds; step `completed` after 3 attempts |

**`test_node_pii_scan.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_no_pii_proceeds` | Scanner mock returns `(False, [])` | Returns `{}`; status unchanged |
| `test_pii_detected_review` | Scanner mock returns `(True, ["PERSON", "PESEL"])` | Returns `{"status": "needs_review"}`; job status `awaiting_review` |
| `test_pii_count_logged_not_values` | Scanner returns PII | `step.meta["flags_count"] == 2`; no PII text in meta |
| `test_scanner_injectable` | Custom mock scanner passed | Uses mock; DefaultPIIScanner not called |

**`test_node_chunk.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_recursive_strategy` | chunk_config = `{"strategy": "recursive", "chunk_size": 100, "overlap": 10}` | Returns chunks with correct max token_count |
| `test_by_section_strategy` | Sections provided in state | Each section becomes at least one chunk |
| `test_deterministic_point_ids` | Same doc_id + chunk_index | `point_id` is identical across two calls |
| `test_document_type_override` | chunk_config has `document_type_overrides.table` with size=256 | Table-type doc uses 256 chunk size |

**`test_node_embed.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_embed_batch` | 70 chunks; LLM mock returns vectors in batches of 32 | 3 LLM calls; 70 vectors returned |
| `test_embed_model_per_collection` | Two collections with different `embedding_model_id` | Each uses its respective model |
| `test_embed_llm_retry` | LLM fails on batch 1, succeeds on retry | Succeeds; step `completed` |

**`test_node_upsert.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_upsert_calls_retrieval_service` | `RetrievalService.upsert_batch` is an AsyncMock | Called with correct `TenantContext` and `points` list |
| `test_upsert_no_direct_qdrant_import` | Import inspection | `node_upsert.py` does not import `qdrant_client` |
| `test_upsert_payload_has_tenant_id` | Inspect call args | Every point payload contains `tenant_id` == `state.tenant_id` |

**`test_node_persist.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_persist_inserts_chunks_registry` | 5 chunks in state | 5 rows in `chunks_registry`; all with correct `tenant_id` |
| `test_persist_idempotent` | Same chunks inserted twice | Second insert is no-op (ON CONFLICT DO NOTHING) |
| `test_persist_sets_document_ready` | Normal flow | `document.status == "ready"` |
| `test_persist_writes_audit_log` | Normal flow | `audit_log` has `action=document.indexed` entry |

### Integration tests (`tests/integration/test_ingest_pipeline.py`)

End-to-end: use testcontainers for Postgres, MinIO, and Qdrant. Mock LLM calls.

| Test | Assert |
|---|---|
| `test_full_pipeline_happy_path` | Document goes from `uploaded` to `ready`; chunks in Qdrant; chunks_registry rows exist |
| `test_pipeline_pauses_on_pii` | Document stops at `needs_review`; checkpoint saved; LangGraph thread_id set |
| `test_pipeline_resumes_after_approval` | Resume from checkpoint at chunk node; document reaches `ready` |
| `test_pipeline_rejects_duplicate` | Second identical doc → `rejected`; no Qdrant points |
| `test_pipeline_failed_after_3_retries` | MinIO intermittently fails 3x | Document → `failed`; `ingestion_jobs.status=failed` |

---

## Definition of Done

- [ ] All 9 node files exist in `src/graphs/ingest_graph/nodes/`.
- [ ] `IngestState` is a Pydantic `BaseModel` with all fields typed.
- [ ] `build_ingest_graph()` compiles without error with a test `AsyncPostgresSaver`.
- [ ] Every node updates `ingestion_jobs.steps` with a valid `IngestStep` entry.
- [ ] `node_pii_scan` accepts an injectable `PIIScanner` protocol (testable in isolation).
- [ ] `node_upsert` imports only `RetrievalService`, not `qdrant_client`.
- [ ] All prompts used in `node_validate` live in `src/graphs/prompts/`.
- [ ] `ruff check --fix . && mypy src/ && pytest tests/unit/ingest_graph/ -x -q` passes green.
- [ ] Integration tests `tests/integration/test_ingest_pipeline.py` pass with testcontainers.
- [ ] `tests/security/test_architecture.py` import linter passes (no Qdrant outside `retrieval/`).
- [ ] `docs/architecture.md` §8 ingest graph diagram matches the implemented topology.
