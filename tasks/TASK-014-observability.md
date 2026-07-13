# TASK-014: Observability Stack Integration

**Status:** TODO
**Priority:** P1 — required before production
**Owner:** backend-dev
**Reviewer:** python-reviewer + security-auditor
**Related docs:** `docs/architecture.md` §14, §17 | `docs/rodo.md`
**Estimated effort:** 3–4 days

---

## Overview

Implement the full observability stack: Langfuse tracing for all LangGraph runs, Prometheus metrics via `prometheus-fastapi-instrumentator`, health check endpoints, 8 Prometheus alert rules, and structured logging with `structlog`.

All integrations must comply with the PII protection rules in `docs/rodo.md` and `.claude/rules/security.md`:
- Langfuse: `capture_input=False`, `capture_output=False` on all spans.
- Logs: no document content, no prompt text, no response text, no PII. Only identifiers and metrics.
- Prometheus labels: only `tenant_id` (UUID string), `collection_id` (UUID string), `intent` (enum), `status` (enum). No free-text labels.

The metrics and alert rules are specified in `docs/architecture.md` §17. This task implements them exactly as defined there.

---

## Usage

### Langfuse tracing

```python
# Automatic in query graph via src/graphs/query_graph/tracing.py
# Automatic in ingest graph via src/graphs/ingest_graph/tracing.py

# Manual span example for a service method:
from src.core.observability.langfuse import get_langfuse_client, create_span

async def some_service_method(tenant_id: UUID, ...):
    with create_span("my_service.operation", metadata={"tenant_id": str(tenant_id)}) as span:
        result = await do_work()
        span.update(result_count=len(result))
    return result
```

### Prometheus metrics

```python
# Auto-instrumented by prometheus-fastapi-instrumentator for HTTP endpoints.
# Custom metrics via src/core/observability/metrics.py:
from src.core.observability.metrics import (
    RAG_QUERY_DURATION,
    RETRIEVAL_SCORE,
    INGEST_JOB_DURATION,
)

RAG_QUERY_DURATION.labels(
    tenant_id=str(tenant_id),
    collection_id=str(collection_id),
    intent="factual",
).observe(latency_seconds)
```

### Health checks

```
GET /health        → 200 OK (liveness — process is running)
GET /health/ready  → 200 OK | 503 (readiness — all dependencies healthy)
```

---

## Tech Stack

- **langfuse** `2.x` — Python SDK; `Langfuse` client, `StatefulSpanClient`, `create_generation`
- **prometheus-fastapi-instrumentator** `0.9+` — auto HTTP metrics (request_count, latency histograms)
- **prometheus-client** `0.20+` — custom metric definitions (`Histogram`, `Gauge`, `Counter`)
- **structlog** `23+` — structured JSON logging; `BoundLogger` pattern
- **asyncpg** / **sqlalchemy async** — Postgres health check
- **qdrant-client** — Qdrant health check (`/readyz` endpoint via httpx)
- **minio-py** — MinIO health check (`/minio/health/live`)
- **redis.asyncio** — Redis health check (`PING`)
- **httpx** — used for Qdrant and MinIO health checks (they expose HTTP endpoints)

---

## Database Patterns

Observability components do not write to application tables directly, except:

**`audit_log`** — Not written by this component. Audit log is written by domain services and routers.

**Langfuse** — Writes to its own self-hosted Postgres database (separate from the RAG API Postgres). The Langfuse SDK sends data via HTTP to the Langfuse server — no direct DB access from the application.

**Redis** — Worker heartbeat key written by ingest worker: `SET worker:heartbeat:{worker_id} {timestamp} EX 120`. This is an observability side-effect, not application data.

---

## Architecture — SOLID & DRY

### File layout

```
src/core/observability/
    __init__.py
    langfuse.py          # Langfuse client singleton + span helpers
    metrics.py           # All custom Prometheus metric definitions
    logging.py           # structlog configuration + context vars
    health.py            # Health check implementations

src/api/routers/
    health.py            # GET /health and GET /health/ready endpoints

src/graphs/query_graph/
    tracing.py           # Query graph span helpers (imports from core/observability)

src/graphs/ingest_graph/
    tracing.py           # Ingest graph span helpers
```

---

## Implementation Steps

### Step 1: Structured logging (`src/core/observability/logging.py`)

Configure structlog at application startup. All log output is JSON for production; pretty-printed for development.

```python
import logging
import structlog
from src.core.config import settings

def configure_logging() -> None:
    """Configure structlog for structured JSON logging.

    Rules (docs/rodo.md + .claude/rules/security.md):
    - Never log document content, prompt text, response text, or any user-provided text.
    - Never log chunk text, query text, or answer text.
    - Log only: identifiers (UUIDs), metrics (counts, latencies, booleans), action codes.
    - Required context fields: tenant_id, user_id (when available), component name.
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.environment == "production":
        processors = shared_processors + [structlog.processors.JSONRenderer()]
    else:
        processors = shared_processors + [structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Set standard library logging level
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
```

**Context binding helper** (for request-scoped context):

```python
from structlog.contextvars import bind_contextvars, clear_contextvars

async def bind_request_context(
    tenant_id: str,
    user_id: str,
    request_id: str,
) -> None:
    """Bind per-request context fields. Called in FastAPI middleware."""
    bind_contextvars(
        tenant_id=tenant_id,
        user_id=user_id,
        request_id=request_id,
    )
```

**FastAPI middleware** (`src/api/middleware/logging.py`):

```python
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.contextvars import bind_contextvars, clear_contextvars
import uuid

class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        clear_contextvars()
        request_id = str(uuid.uuid4())
        # Bind what we know before auth (no user_id yet)
        bind_contextvars(request_id=request_id, path=request.url.path)
        response = await call_next(request)
        return response
```

After auth dependency resolves, the router or service binds `tenant_id` and `user_id`.

### Step 2: Prometheus metrics definitions (`src/core/observability/metrics.py`)

```python
from prometheus_client import Histogram, Gauge, Counter

# Per docs/architecture.md §17 — exact metric names and labels as specified

RAG_QUERY_DURATION = Histogram(
    name="rag_query_duration_seconds",
    documentation="End-to-end query graph latency in seconds",
    labelnames=["tenant_id", "collection_id", "intent"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

RAG_RETRIEVAL_SCORE = Histogram(
    name="rag_retrieval_score",
    documentation="Qdrant retrieval score distribution per query",
    labelnames=["tenant_id", "collection_id"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

INGEST_JOB_DURATION = Histogram(
    name="ingest_job_duration_seconds",
    documentation="Per-stage ingest graph node latency in seconds",
    labelnames=["tenant_id", "stage"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

INGEST_DLQ_SIZE = Gauge(
    name="ingest_dlq_size",
    documentation="Number of events in the dead-letter queue (ingest_events_dlq)",
)

ACTIVE_CONVERSATIONS = Gauge(
    name="active_conversations_total",
    documentation="Number of non-deleted conversations per tenant",
    labelnames=["tenant_id"],
)

DOCUMENT_STATUS = Gauge(
    name="document_status_total",
    documentation="Count of documents per status per tenant",
    labelnames=["tenant_id", "status"],
)

# Worker heartbeat (ingest worker exposes this)
WORKER_LAST_HEARTBEAT = Gauge(
    name="ingest_worker_last_heartbeat_timestamp",
    documentation="Unix timestamp of last worker heartbeat",
    labelnames=["worker_id"],
)

# Additional operational counters
INGEST_JOB_FAILED = Counter(
    name="ingest_job_failed_total",
    documentation="Total failed ingest jobs",
    labelnames=["tenant_id", "stage"],
)

LLM_TOKENS_USED = Counter(
    name="llm_tokens_used_total",
    documentation="Total LLM tokens consumed per tenant per model",
    labelnames=["tenant_id", "model_id", "token_type"],  # token_type: prompt|completion
)
```

**Usage in query graph node_persist:**

```python
from src.core.observability.metrics import (
    RAG_QUERY_DURATION, RAG_RETRIEVAL_SCORE, LLM_TOKENS_USED
)

# In node_persist (after graph completes):
RAG_QUERY_DURATION.labels(
    tenant_id=str(state.user_ctx.tenant_id),
    collection_id=str(state.pipeline_config.collection_ids[0]),
    intent=state.intent or "unknown",
).observe(state.latency_ms / 1000)

for chunk in state.graded_chunks:
    RAG_RETRIEVAL_SCORE.labels(
        tenant_id=str(state.user_ctx.tenant_id),
        collection_id=str(chunk.chunk.collection_id),
    ).observe(chunk.chunk.score)

LLM_TOKENS_USED.labels(
    tenant_id=str(state.user_ctx.tenant_id),
    model_id=str(state.pipeline_config.llm_model_id),
    token_type="prompt",
).inc(state.prompt_tokens)
LLM_TOKENS_USED.labels(
    tenant_id=str(state.user_ctx.tenant_id),
    model_id=str(state.pipeline_config.llm_model_id),
    token_type="completion",
).inc(state.completion_tokens)
```

**Usage in ingest graph node_persist:**

```python
from src.core.observability.metrics import INGEST_JOB_DURATION

# Record per-stage latencies from ingestion_jobs.steps
for step in job.steps:
    if step["status"] == "completed" and step.get("completed_at") and step.get("started_at"):
        duration = (parse(step["completed_at"]) - parse(step["started_at"])).total_seconds()
        INGEST_JOB_DURATION.labels(
            tenant_id=str(state.tenant_id),
            stage=step["stage"],
        ).observe(duration)
```

**DLQ size gauge** — updated by a periodic background task in the ingest worker (every 30s):

```python
# src/ingest/worker.py — heartbeat + DLQ metrics loop
async def _metrics_loop(redis: Redis) -> None:
    while True:
        dlq_len = await redis.xlen("ingest_events_dlq")
        INGEST_DLQ_SIZE.set(dlq_len)

        # Heartbeat
        worker_id = f"{socket.gethostname()}-{os.getpid()}"
        await redis.set(f"worker:heartbeat:{worker_id}", int(time.time()), ex=120)
        WORKER_LAST_HEARTBEAT.labels(worker_id=worker_id).set(time.time())

        await asyncio.sleep(30)
```

**Document status and conversation gauges** — updated by a periodic task (every 60s):

```python
async def _refresh_status_gauges(db: AsyncSession) -> None:
    """Refresh document_status_total and active_conversations_total gauges."""
    # Document status counts
    rows = await db.execute(
        select(Document.tenant_id, Document.status, func.count())
        .group_by(Document.tenant_id, Document.status)
    )
    for tenant_id, status, count in rows:
        DOCUMENT_STATUS.labels(
            tenant_id=str(tenant_id),
            status=status,
        ).set(count)

    # Active conversation counts
    rows = await db.execute(
        select(Conversation.tenant_id, func.count())
        .where(Conversation.is_deleted == False)
        .group_by(Conversation.tenant_id)
    )
    for tenant_id, count in rows:
        ACTIVE_CONVERSATIONS.labels(tenant_id=str(tenant_id)).set(count)
```

### Step 3: Prometheus instrumentation in FastAPI

```python
# src/api/app.py
from prometheus_fastapi_instrumentator import Instrumentator

def create_app() -> FastAPI:
    app = FastAPI(title="RAG API", version="0.2.0")

    # Auto HTTP metrics: http_request_duration_seconds, http_requests_total
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        should_instrument_requests_inprogress=True,
        excluded_handlers=["/health", "/health/ready", "/metrics"],
        env_var_name="ENABLE_METRICS",
        inprogress_name="http_requests_inprogress",
        inprogress_labels=True,
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app
```

`/metrics` endpoint is on port 8000 (same process) but excluded from routing middleware. Per `docs/architecture.md` §17: the endpoint is not authenticated; it is only reachable on the internal Docker network.

### Step 4: Health check endpoints (`src/core/observability/health.py`)

```python
from dataclasses import dataclass
from enum import Enum
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

@dataclass
class ComponentHealth:
    name: str
    status: HealthStatus
    latency_ms: float | None = None
    error: str | None = None

async def check_postgres(db: AsyncSession) -> ComponentHealth:
    import time
    start = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
        return ComponentHealth(
            name="postgres",
            status=HealthStatus.HEALTHY,
            latency_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as exc:
        return ComponentHealth(
            name="postgres",
            status=HealthStatus.UNHEALTHY,
            error=type(exc).__name__,   # no message (may contain connection string)
        )

async def check_qdrant(qdrant_url: str) -> ComponentHealth:
    import time
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{qdrant_url}/readyz")
            resp.raise_for_status()
        return ComponentHealth(
            name="qdrant",
            status=HealthStatus.HEALTHY,
            latency_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as exc:
        return ComponentHealth(
            name="qdrant",
            status=HealthStatus.UNHEALTHY,
            error=type(exc).__name__,
        )

async def check_redis(redis: Redis) -> ComponentHealth:
    import time
    start = time.monotonic()
    try:
        await redis.ping()
        return ComponentHealth(
            name="redis",
            status=HealthStatus.HEALTHY,
            latency_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as exc:
        return ComponentHealth(
            name="redis",
            status=HealthStatus.UNHEALTHY,
            error=type(exc).__name__,
        )

async def check_minio(minio_url: str) -> ComponentHealth:
    import time
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{minio_url}/minio/health/live")
            resp.raise_for_status()
        return ComponentHealth(
            name="minio",
            status=HealthStatus.HEALTHY,
            latency_ms=(time.monotonic() - start) * 1000,
        )
    except Exception as exc:
        return ComponentHealth(
            name="minio",
            status=HealthStatus.UNHEALTHY,
            error=type(exc).__name__,
        )

async def get_readiness(
    db: AsyncSession,
    redis: Redis,
    qdrant_url: str,
    minio_url: str,
) -> tuple[bool, list[ComponentHealth]]:
    """Check all dependencies. Returns (is_ready, component_statuses)."""
    checks = await asyncio.gather(
        check_postgres(db),
        check_qdrant(qdrant_url),
        check_redis(redis),
        check_minio(minio_url),
        return_exceptions=False,
    )
    is_ready = all(c.status == HealthStatus.HEALTHY for c in checks)
    return is_ready, list(checks)
```

**Health router (`src/api/routers/health.py`):**

```python
from fastapi import APIRouter, Depends, status, Response
from src.core.observability.health import get_readiness, HealthStatus

router = APIRouter(tags=["health"])

@router.get("/health")
async def liveness():
    """Liveness probe: returns 200 if the process is running.

    Used by Docker health check: curl -f http://localhost:8000/health
    Does NOT check dependencies — only verifies the process responds.
    """
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """Readiness probe: returns 200 if all dependencies are healthy, 503 otherwise.

    Checks: Postgres, Qdrant, Redis, MinIO.
    Used by load balancer to route traffic only to healthy instances.
    """
    is_ready, components = await get_readiness(
        db=db,
        redis=redis,
        qdrant_url=settings.qdrant_url,
        minio_url=settings.minio_url,
    )

    body = {
        "status": "ready" if is_ready else "not_ready",
        "components": [
            {
                "name": c.name,
                "status": c.status,
                "latency_ms": c.latency_ms,
                # error field omitted in healthy case; included on failure
                **({"error": c.error} if c.status != HealthStatus.HEALTHY else {}),
            }
            for c in components
        ],
    }

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return body
```

### Step 5: Langfuse integration (`src/core/observability/langfuse.py`)

```python
from langfuse import Langfuse
from contextlib import contextmanager, asynccontextmanager
from src.core.config import settings

_client: Langfuse | None = None

def get_langfuse_client() -> Langfuse:
    """Singleton Langfuse client. Configured from LANGFUSE_* env vars."""
    global _client
    if _client is None:
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
            enabled=settings.langfuse_enabled,
        )
    return _client


def create_trace(
    name: str,
    trace_id: str,
    user_id: str,
    tenant_id: str,
    session_id: str | None = None,
) -> "StatefulTraceClient":
    """Create a Langfuse trace for a query or ingest run.

    Args:
        name: Trace name ("query_graph" or "ingest_graph").
        trace_id: Unique trace ID (message_id for queries, ingestion_job_id for ingest).
        user_id: User identifier (UUID string). NOT email or name.
        tenant_id: Tenant identifier (UUID string).
        session_id: Conversation ID for query traces.

    SECURITY: Never pass message content, document text, or PII as metadata.
    """
    return get_langfuse_client().trace(
        id=trace_id,
        name=name,
        user_id=user_id,
        session_id=session_id,
        metadata={
            "tenant_id": tenant_id,
            # No content here — only structural identifiers
        },
        # PII protection: disable automatic input/output capture
        input=None,
        output=None,
    )


@contextmanager
def create_span(
    name: str,
    trace: "StatefulTraceClient",
    metadata: dict | None = None,
):
    """Context manager for a Langfuse span within a trace.

    SECURITY: capture_input and capture_output are always False.
    metadata must not contain content, text, or PII.
    """
    span = trace.span(
        name=name,
        metadata=metadata or {},
        input=None,     # explicit None — no input capture
        output=None,    # explicit None — no output capture
    )
    try:
        yield span
        span.end()
    except Exception as exc:
        span.update(level="ERROR", status_message=type(exc).__name__)
        span.end()
        raise


def create_generation(
    name: str,
    trace: "StatefulTraceClient",
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: float,
) -> None:
    """Record an LLM generation within a trace.

    SECURITY: input (prompt) and output (response) are always None.
    Only token counts and latency are recorded — no content.
    """
    trace.generation(
        name=name,
        model=model,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        latency=latency_ms,
        input=None,   # NEVER send prompt content to Langfuse
        output=None,  # NEVER send response content to Langfuse
    )
```

**Query graph tracing (`src/graphs/query_graph/tracing.py`):**

```python
from src.core.observability.langfuse import create_trace, create_span, create_generation
from src.graphs.query_graph.state import QueryState

def start_query_trace(state: QueryState) -> "StatefulTraceClient":
    """Create the root Langfuse trace for a query graph run."""
    return create_trace(
        name="query_graph",
        trace_id=str(state.message_id),
        user_id=str(state.user_ctx.user_id),
        tenant_id=str(state.user_ctx.tenant_id),
        session_id=str(state.conversation_id),
    )
```

**Ingest graph tracing (`src/graphs/ingest_graph/tracing.py`):**

```python
from src.core.observability.langfuse import create_trace, create_span
from src.graphs.ingest_graph.state import IngestState

def start_ingest_trace(state: IngestState, job_id: str) -> "StatefulTraceClient":
    """Create the root Langfuse trace for an ingest graph run."""
    return create_trace(
        name="ingest_graph",
        trace_id=job_id,
        user_id="system",       # ingest is a system action
        tenant_id=str(state.tenant_id),
        session_id=None,
    )
```

**Full span hierarchy for query graph:**

```
Trace: query_graph (trace_id = message_id, session_id = conversation_id)
  Generation: classify_intent  (model, prompt_tokens, completion_tokens, latency_ms)
  Generation: rewrite_query    (model, prompt_tokens, completion_tokens, latency_ms)
  Span: retrieve               (metadata: collection_ids, top_k, score_threshold, chunks_returned)
  Generation: grade_documents  (model, prompt_tokens, completion_tokens, chunks_relevant, chunks_total)
  [Optional] Generation: refine_query
  Generation: generate         (model, prompt_tokens, completion_tokens, latency_ms)
  Span: guardrails_output      (metadata: pii_found, disclaimer_added)
  Span: persist                (metadata: latency_ms, sources_count)
```

**Full span hierarchy for ingest graph:**

```
Trace: ingest_graph (trace_id = job_id, user_id = "system")
  Span: fetch_from_minio    (metadata: size_bytes, latency_ms)
  Span: extract_text        (metadata: page_count, word_count, latency_ms)
  Span: dedupe_check        (metadata: result, latency_ms)
  Generation: llm_validate  (model, prompt_tokens, completion_tokens, category, confidence, latency_ms)
  Span: pii_scan            (metadata: pii_found, flags_count, latency_ms)
  Span: chunk               (metadata: strategy, chunk_count, latency_ms)
  Generation: embed         (model, batch_count, latency_ms)  -- not a generation strictly, but token usage tracked
  Span: upsert_qdrant       (metadata: points_upserted, latency_ms)
  Span: persist_status      (metadata: chunks_registry_rows, latency_ms)
```

### Step 6: Alert rules (`src/core/observability/alerts.yml`)

Prometheus alert rules matching `docs/architecture.md` §17. These are applied to the Prometheus instance via `docker-compose.yml` config mount.

```yaml
# src/core/observability/alerts.yml
# Applied as: prometheus.yml scrape_configs + rule_files: ["/etc/prometheus/alerts.yml"]
groups:
  - name: rag_platform_alerts
    interval: 1m
    rules:

      # 1. API p95 latency > 5 seconds
      - alert: QueryLatencyHigh
        expr: |
          histogram_quantile(0.95,
            rate(http_request_duration_seconds_bucket{
              job="rag-api", handler="/v1/chat/completions"
            }[5m])
          ) > 5
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "RAG API p95 latency is high"
          description: "95th percentile latency for /v1/chat/completions exceeds 5 seconds for 2 minutes. Current: {{ $value | humanizeDuration }}"
          runbook: "Check LLM GPU load, Qdrant search latency, and Postgres query performance."

      # 2. Ingest queue depth > 100
      - alert: IngestQueueBacklog
        expr: redis_stream_length{stream="ingest_events"} > 100
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Ingest queue depth is high"
          description: "ingest_events stream has {{ $value }} pending events. Workers may be slow or crashed."
          runbook: "Check ingest worker health: GET /health on worker port 9090. Check worker logs for errors."

      # 3. DLQ has any events
      - alert: DLQAlert
        expr: redis_stream_length{stream="ingest_events_dlq"} > 0
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "Dead-letter queue has events"
          description: "{{ $value }} events in ingest_events_dlq. Documents have failed processing after 3 retries."
          runbook: "Check document status=failed in admin panel. POST /documents/{id}/reindex to retry."

      # 4. Retrieval score degradation (avg score drops below 0.4)
      - alert: RetrievalScoreDrop
        expr: |
          avg(rate(rag_retrieval_score_sum[10m]) / rate(rag_retrieval_score_count[10m])) < 0.4
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "RAG retrieval score has dropped"
          description: "Average retrieval score over 10 minutes is {{ $value | humanize }}. Expected >= 0.4."
          runbook: "Check embedding model health. Verify Qdrant collection is not empty. Check for query pattern changes."

      # 5. LLM token quota nearing (tenant-level — uses LLM_TOKENS_USED counter)
      - alert: TokenQuotaNearing
        expr: |
          increase(llm_tokens_used_total[24h]) > 900000
        for: 0m
        labels:
          severity: info
        annotations:
          summary: "LLM token usage is nearing daily threshold"
          description: "{{ $labels.tenant_id }} has used {{ $value | humanize }} tokens in the last 24 hours."
          runbook: "Review token usage per pipeline in Grafana 'LLM Usage' dashboard."

      # 6. Keycloak unreachable
      - alert: KeycloakDown
        expr: up{job="keycloak"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Keycloak is down"
          description: "Keycloak OIDC provider is unreachable. All authentication will fail."
          runbook: "Check Keycloak container: docker compose ps keycloak. Check health: GET /health/ready on Keycloak."

      # 7. Qdrant unreachable
      - alert: QdrantDown
        expr: up{job="qdrant"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Qdrant vector database is down"
          description: "Qdrant is unreachable. All RAG queries will fail at the retrieval step."
          runbook: "Check Qdrant container. Qdrant health: GET http://qdrant:6333/readyz"

      # 8. MinIO unreachable
      - alert: MinIODown
        expr: up{job="minio"} == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "MinIO object storage is down"
          description: "MinIO is unreachable. Document uploads and ingest fetches will fail."
          runbook: "Check MinIO container. MinIO health: GET http://minio:9000/minio/health/live"
```

### Step 7: Prometheus scrape configuration

```yaml
# Referenced from docker-compose.yml as prometheus config
# File: src/core/observability/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 1m

rule_files:
  - "/etc/prometheus/alerts.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]

scrape_configs:
  - job_name: "rag-api"
    static_configs:
      - targets: ["rag-api:8000"]
    metrics_path: "/metrics"

  - job_name: "ingest-worker"
    static_configs:
      - targets: ["ingest-worker:9090"]
    metrics_path: "/metrics"

  - job_name: "qdrant"
    static_configs:
      - targets: ["qdrant:6333"]
    metrics_path: "/metrics"

  - job_name: "redis"
    static_configs:
      - targets: ["redis-exporter:9121"]

  - job_name: "postgres"
    static_configs:
      - targets: ["postgres-exporter:9187"]

  - job_name: "minio"
    static_configs:
      - targets: ["minio:9000"]
    metrics_path: "/metrics"

  - job_name: "keycloak"
    static_configs:
      - targets: ["keycloak:8080"]
    metrics_path: "/metrics"
```

---

## API Contracts

### GET /health

**Auth:** None required.

**Response 200:**
```json
{"status": "ok"}
```

### GET /health/ready

**Auth:** None required.

**Response 200 (all healthy):**
```json
{
  "status": "ready",
  "components": [
    {"name": "postgres", "status": "healthy", "latency_ms": 2.1},
    {"name": "qdrant",   "status": "healthy", "latency_ms": 5.3},
    {"name": "redis",    "status": "healthy", "latency_ms": 0.8},
    {"name": "minio",    "status": "healthy", "latency_ms": 12.4}
  ]
}
```

**Response 503 (any dependency unhealthy):**
```json
{
  "status": "not_ready",
  "components": [
    {"name": "postgres", "status": "healthy", "latency_ms": 2.1},
    {"name": "qdrant",   "status": "unhealthy", "error": "ConnectionRefusedError"},
    {"name": "redis",    "status": "healthy", "latency_ms": 0.8},
    {"name": "minio",    "status": "healthy", "latency_ms": 12.4}
  ]
}
```

Note: `error` values are exception class names only — no connection string, no credentials, no server details.

---

## Security Checklist

- [ ] Langfuse `input=None` and `output=None` on every `trace()`, `span()`, and `generation()` call. No prompt text, response text, or chunk content ever reaches Langfuse.
- [ ] Langfuse `metadata` fields contain only UUIDs, counts, booleans, and model names. No free-text from user queries or document content.
- [ ] `/health/ready` error fields contain only exception class names — no connection strings, passwords, or internal paths.
- [ ] `/metrics` is excluded from the reverse proxy routing — not accessible from the internet. Internal network only per `docs/architecture.md` §17.
- [ ] Prometheus label values use only UUIDs and enums — no user-provided free text that could create cardinality explosion or PII leakage in metric labels.
- [ ] `structlog` configuration: `logging.getLogger("sqlalchemy.engine").setLevel(WARNING)` — prevents SQL query logging (queries may contain UUIDs and parameter values).
- [ ] Worker heartbeat Redis key uses `worker_id = "{hostname}-{pid}"` — no tenant_id or user data in the key name.
- [ ] `INGEST_DLQ_SIZE` gauge uses `XLEN` — no event content is read.
- [ ] Health check for MinIO uses only the `/minio/health/live` endpoint — no credentials in the health check URL. Authentication for MinIO operations is separate.
- [ ] Alertmanager receiver configuration (Slack webhook URL, PagerDuty key) must be in environment variables, never in `alerts.yml` file committed to the repo.

---

## Terms of Use (relevant constraints)

- **8 alert rules exactly**: `QueryLatencyHigh`, `IngestQueueBacklog`, `DLQAlert`, `RetrievalScoreDrop`, `TokenQuotaNearing`, `KeycloakDown`, `QdrantDown`, `MinIODown`. Names must match `docs/architecture.md` §17 exactly for Grafana dashboards to reference them.
- **Langfuse PII masking**: the Langfuse server itself is configured with `LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES=false` and the SDK is configured at write time with `input=None`, `output=None`. Both layers are required — one is not sufficient.
- **No content in Prometheus labels**: metric labels are indexed and retained indefinitely by Prometheus. Putting free-text (questions, answers, document names) in labels would create GDPR-violating retention.
- **`/metrics` authentication exemption**: the endpoint is unauthenticated by design — it is inside the internal Docker network, not exposed via the reverse proxy. This is the standard Prometheus pattern. Do not add bearer auth to `/metrics` — it breaks Prometheus scraping.
- **`structlog` not for PII auditing**: application logs are for operations (debugging, performance monitoring). Audit trails go to `audit_log` in Postgres. Never route GDPR-relevant events through the log pipeline.

---

## Tests

### Unit tests (`tests/unit/`)

**`tests/unit/observability/test_health.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_liveness_always_returns_200` | No dependencies needed | GET /health → 200 `{"status": "ok"}` |
| `test_readiness_all_healthy` | All dependency mocks return healthy | GET /health/ready → 200 `{"status": "ready"}` |
| `test_readiness_postgres_down` | Postgres mock raises | 503; `components` has `postgres` with `status=unhealthy` |
| `test_readiness_qdrant_down` | Qdrant mock raises | 503; `components` has `qdrant` with `status=unhealthy` |
| `test_readiness_error_field_no_connection_string` | Any dependency raises with connection string in message | `error` field contains only exception class name |
| `test_readiness_partial_failure` | 3 of 4 healthy, 1 unhealthy | 503; 3 components healthy, 1 unhealthy |

**`tests/unit/observability/test_langfuse.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_create_trace_no_input_no_output` | Create trace via `create_trace()` | Langfuse client mock called with `input=None`, `output=None` |
| `test_create_span_no_input_no_output` | Create span via `create_span()` | Span mock called with `input=None`, `output=None` |
| `test_create_generation_no_content` | Create generation | Generation mock called with `input=None`, `output=None` |
| `test_span_metadata_no_content_fields` | Inspect metadata dict | No key `text`, `content`, `answer`, `question`, `prompt` in metadata |
| `test_span_ends_on_exception` | Span context raises | `span.end()` still called; level `ERROR` |
| `test_trace_user_id_is_uuid_not_email` | Create trace with user_id | `user_id` in call args is UUID string format |

**`tests/unit/observability/test_metrics.py`**

| Test | Setup | Assert |
|---|---|---|
| `test_rag_query_duration_labels` | `RAG_QUERY_DURATION.labels(tenant_id, collection_id, intent).observe(1.0)` | Metric registered with correct label values |
| `test_retrieval_score_histogram` | Observe 10 scores | Histogram buckets populated |
| `test_ingest_dlq_size_gauge` | `INGEST_DLQ_SIZE.set(5)` | Gauge value is 5 |
| `test_document_status_gauge_labels` | Set gauge for 3 statuses | All 3 label combinations registered |

### Integration tests (`tests/integration/test_observability.py`)

| Test | Assert |
|---|---|
| `test_metrics_endpoint_accessible` | GET /metrics → 200; content-type is `text/plain; charset=utf-8` |
| `test_metrics_contains_http_duration` | After one API call | `http_request_duration_seconds_bucket` in metrics output |
| `test_health_ready_with_testcontainers` | Postgres, Redis, Qdrant testcontainers running | GET /health/ready → 200 |
| `test_health_ready_qdrant_stopped` | Qdrant container stopped | GET /health/ready → 503 |

---

## Definition of Done

- [ ] `src/core/observability/langfuse.py` with `create_trace()`, `create_span()`, `create_generation()` — all with `input=None`, `output=None`.
- [ ] `src/core/observability/metrics.py` with all 7 custom metric definitions matching `docs/architecture.md` §17.
- [ ] `prometheus-fastapi-instrumentator` installed and wired in `create_app()`.
- [ ] `GET /health` returns 200 always (liveness).
- [ ] `GET /health/ready` checks Postgres, Qdrant, Redis, MinIO; returns 200/503 correctly.
- [ ] Health check error fields contain only exception class names — no sensitive information.
- [ ] `src/core/observability/alerts.yml` contains exactly 8 alert rules with names matching `docs/architecture.md` §17.
- [ ] `structlog` configured with JSON output in production, `clear_contextvars()` called per request.
- [ ] DLQ size gauge updated every 30s by worker metrics loop.
- [ ] Document status and conversation gauges updated every 60s by background task.
- [ ] Langfuse tracing verified by unit tests: no content in span attributes.
- [ ] `ruff check --fix . && mypy src/core/observability/ && pytest tests/unit/observability/ -x -q` passes.
- [ ] Integration tests with testcontainers pass for health endpoints.
