# TASK-009: RetrievalService

**Status:** TODO
**Priority:** P0 — blocker for query graph and ingest graph
**Owner:** backend-dev
**Reviewer:** python-reviewer (security profile) + security-auditor
**Related docs:** `docs/architecture.md` §9, §12 (ADR-1) | `docs/data-model.md` §3 | `docs/rodo.md`
**Estimated effort:** 2–3 days

---

## Overview

Implement `RetrievalService` in `src/retrieval/service.py`. This is the **single and exclusive** module allowed to instantiate a Qdrant client and execute vector queries. This constraint is a hard architectural rule defined in `docs/architecture.md` §9 (ADR-1) and enforced by the import linter in `tests/security/test_architecture.py`.

The service enforces two invariants on every Qdrant call without exception:
1. `tenant_id` filter — isolates data between tenants.
2. `collection_id` filter — enforces collection-level RBAC (only collections the user's roles grant access to).

Every method receives a `TenantContext` that carries these values. There is no code path that queries Qdrant without the mandatory filter.

Qdrant collection naming follows ADR-1: one physical collection per embedding model (`emb_bge_m3`), not per tenant. The `tenant_id` payload field is the isolation boundary.

---

## Usage

```python
from src.retrieval.service import RetrievalService
from src.retrieval.schemas import TenantContext, RetrievalResult, QdrantPoint

# Injected as a FastAPI dependency or passed by the query graph node
svc = RetrievalService(qdrant_client=client)

# Search
results: list[RetrievalResult] = await svc.search(
    ctx=TenantContext(
        tenant_id=user_ctx.tenant_id,
        allowed_collection_ids=user_ctx.allowed_collection_ids,
    ),
    query_vector=[0.1, 0.2, ...],  # 1024-dim
    top_k=8,
    score_threshold=0.35,
)

# Upsert during ingest
await svc.upsert_batch(ctx, points=[...])

# GDPR cascade deletion
deleted_count: int = await svc.delete_by_document(ctx, document_id=doc_id)
```

---

## Tech Stack

- **qdrant-client** `1.9+` — async client (`AsyncQdrantClient`) for vector operations
- **pydantic** v2 — `TenantContext`, `RetrievalResult`, `QdrantPoint` schemas
- **tenacity** — retry with jitter per `docs/architecture.md` §16 (Qdrant search: 2 retries, 1s fixed; upsert: 3 retries, exp backoff 1-10s)
- **structlog** — structured logging (document_id, tenant_id as context; no vector content)

---

## Database Patterns

`RetrievalService` does NOT interact with Postgres directly. It is a pure Qdrant abstraction. Postgres interactions (looking up collection → embedding model → Qdrant collection name) happen in the calling layer (ingest node or query graph node) before calling the service.

The `QdrantPoint.payload` dict carries all metadata needed for retrieval context. The service does not interpret payload fields beyond what is needed for filter construction.

---

## Architecture — SOLID & DRY

### File layout

```
src/retrieval/
    __init__.py
    service.py        # RetrievalService class (this task)
    schemas.py        # TenantContext, RetrievalResult, QdrantPoint
    filters.py        # _build_mandatory_filter() — single filter construction point
    reranker.py       # Phase 3 placeholder (cross-encoder reranking)
    exceptions.py     # RetrievalError, EmptyCollectionListError
```

### Schemas (`schemas.py`)

```python
from uuid import UUID
from pydantic import BaseModel, Field, model_validator

class TenantContext(BaseModel):
    """Carries tenant isolation and RBAC data. Always derived from JWT — never from request body."""
    tenant_id: UUID
    allowed_collection_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_at_least_one_collection_for_search(self) -> "TenantContext":
        # Upsert and delete operations do not require allowed_collection_ids.
        # Search operations validate this at call time.
        return self


class RetrievalResult(BaseModel):
    point_id: UUID
    document_id: UUID
    chunk_id: UUID | None       # chunks_registry.id; None if not found in Postgres
    score: float
    payload: dict               # full Qdrant payload (text, page, section, etc.)
    page_number: int | None = None
    highlight_text: str | None = None
    collection_id: UUID | None = None


class QdrantPoint(BaseModel):
    id: UUID                    # deterministic: uuid5(doc_id, chunk_index)
    vector: list[float]
    payload: dict               # must include tenant_id, collection_id, document_id
```

### Filter construction (`filters.py`)

This is the single place where the mandatory filter is built. Centralizing it ensures no future code path bypasses the isolation constraint:

```python
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

def build_mandatory_filter(
    tenant_id: str,
    collection_ids: list[str],
) -> Filter:
    """Build the mandatory tenant+RBAC filter for every Qdrant query.

    Args:
        tenant_id: Tenant UUID as string.
        collection_ids: List of collection UUIDs the user is allowed to access.

    Returns:
        A Qdrant Filter with MUST conditions for tenant isolation and collection RBAC.

    Raises:
        EmptyCollectionListError: If collection_ids is empty — prevents full-tenant scan.
    """
    from src.retrieval.exceptions import EmptyCollectionListError
    if not collection_ids:
        raise EmptyCollectionListError(
            "search requires at least one allowed collection; "
            "user has no collection access"
        )
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="collection_id", match=MatchAny(any=collection_ids)),
        ]
    )
```

### RetrievalService class (`service.py`)

```python
from uuid import UUID
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue
from src.retrieval.schemas import TenantContext, RetrievalResult, QdrantPoint
from src.retrieval.filters import build_mandatory_filter
from src.retrieval.exceptions import RetrievalError
import structlog

log = structlog.get_logger(__name__)

class RetrievalService:
    """Single access point to Qdrant. Enforces tenant isolation and collection RBAC.

    All public methods require a TenantContext derived from a verified JWT.
    No method may be called without one.
    """

    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    async def search(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        top_k: int = 8,
        score_threshold: float = 0.0,
        additional_filter: Filter | None = None,
    ) -> list[RetrievalResult]:
        ...

    async def upsert_batch(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        points: list[QdrantPoint],
    ) -> None:
        ...

    async def delete_by_document(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        document_id: UUID,
    ) -> int:
        ...

    async def delete_by_tenant(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
    ) -> int:
        ...

    async def ensure_collection(
        self,
        embedding_model_slug: str,
        vector_size: int,
        distance: str = "Cosine",
    ) -> None:
        """
        Create Qdrant collection if it does not exist.
        Collection name: f"emb_{embedding_model_slug}"
        Called by CollectionService on collection creation.
        Idempotent — safe to call multiple times.
        """
        ...
```

---

## Implementation Steps

### Step 1: Schemas and exceptions

Create `src/retrieval/schemas.py` with `TenantContext`, `RetrievalResult`, `QdrantPoint`.

Create `src/retrieval/exceptions.py`:

```python
class RetrievalError(Exception):
    """Base exception for retrieval layer."""

class EmptyCollectionListError(RetrievalError):
    """Raised when search is attempted with an empty allowed_collection_ids list.

    This is a security invariant: an empty list would cause Qdrant to scan across
    all tenants' collections, violating tenant isolation.
    """

class QdrantUnavailableError(RetrievalError):
    """Raised when all retry attempts to Qdrant are exhausted."""
```

### Step 2: Filter builder

Implement `src/retrieval/filters.py` with `build_mandatory_filter()` as specified above.

Add an overload `build_mandatory_filter_for_delete(tenant_id, document_id)` used by deletion methods:

```python
def build_document_delete_filter(
    tenant_id: str,
    document_id: str,
) -> Filter:
    """Build filter for deleting all points belonging to one document within a tenant."""
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="document_id", match=MatchValue(value=document_id)),
        ]
    )

def build_tenant_delete_filter(tenant_id: str) -> Filter:
    """Build filter for deleting ALL points for a tenant. Owner/system only."""
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
        ]
    )
```

### Step 3: `search()` method

```python
async def search(
    self,
    ctx: TenantContext,
    qdrant_collection: str,
    query_vector: list[float],
    top_k: int = 8,
    score_threshold: float = 0.0,
    additional_filter: Filter | None = None,
) -> list[RetrievalResult]:
    """Search for semantically similar chunks within tenant+collections scope.

    Args:
        ctx: Tenant context from verified JWT. Must have at least one allowed_collection_id.
        qdrant_collection: Physical Qdrant collection name (e.g., "emb_bge_m3").
        query_vector: Embedding vector from the same model used to index the collection.
        top_k: Maximum results to return. Default 8 (configurable per pipeline).
        score_threshold: Minimum cosine similarity score. Default 0.0 (no cutoff).
        additional_filter: Optional extra filter (e.g., category, language). Merged with mandatory filter.

    Returns:
        Ordered list of RetrievalResult (highest score first).

    Raises:
        EmptyCollectionListError: If ctx.allowed_collection_ids is empty.
        QdrantUnavailableError: If Qdrant is unreachable after retries.
    """
    mandatory = build_mandatory_filter(
        tenant_id=str(ctx.tenant_id),
        collection_ids=[str(c) for c in ctx.allowed_collection_ids],
    )

    # Merge additional_filter into mandatory
    combined = _merge_filters(mandatory, additional_filter)

    log.debug("retrieval.search",
              tenant_id=str(ctx.tenant_id),
              collection_count=len(ctx.allowed_collection_ids),
              top_k=top_k,
              score_threshold=score_threshold)

    try:
        hits = await self._client.search(
            collection_name=qdrant_collection,
            query_vector=query_vector,
            query_filter=combined,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
    except Exception as exc:
        raise QdrantUnavailableError(str(exc)) from exc

    return [
        RetrievalResult(
            point_id=UUID(str(hit.id)),
            document_id=UUID(hit.payload["document_id"]),
            chunk_id=None,              # resolved by caller from chunks_registry
            score=hit.score,
            payload=hit.payload,
            page_number=hit.payload.get("page"),
            highlight_text=hit.payload.get("text"),   # full chunk text for grading
            collection_id=UUID(hit.payload["collection_id"]),
        )
        for hit in hits
    ]
```

Wrap the `self._client.search(...)` call with tenacity retry:

```python
from tenacity import retry, stop_after_attempt, wait_fixed, add_jitter, retry_if_exception_type

@retry(
    stop=stop_after_attempt(2),
    wait=add_jitter(wait_fixed(1), 0.1),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _qdrant_search(self, ...) -> list:
    ...
```

Per `docs/architecture.md` §16: Qdrant search — 2 retry attempts, fixed 1s delay ±10% jitter.

### Step 4: `upsert_batch()` method

```python
async def upsert_batch(
    self,
    ctx: TenantContext,
    qdrant_collection: str,
    points: list[QdrantPoint],
) -> None:
    """Upsert a batch of points into Qdrant. Idempotent via deterministic point IDs.

    Args:
        ctx: Tenant context. Used only for audit logging.
        qdrant_collection: Physical Qdrant collection name.
        points: List of QdrantPoint. Each point's payload MUST include tenant_id,
                collection_id, and document_id. Validated before upsert.

    Raises:
        ValueError: If any point payload is missing required fields.
        QdrantUnavailableError: If Qdrant is unreachable after retries.
    """
    for point in points:
        _validate_point_payload(ctx, point)   # raises ValueError on tenant_id mismatch

    qdrant_points = [
        PointStruct(
            id=str(p.id),
            vector=p.vector,
            payload=p.payload,
        )
        for p in points
    ]

    log.debug("retrieval.upsert_batch",
              tenant_id=str(ctx.tenant_id),
              count=len(qdrant_points),
              collection=qdrant_collection)

    await self._client.upsert(
        collection_name=qdrant_collection,
        points=qdrant_points,
        wait=True,          # synchronous confirmation
    )
```

Retry policy per §16: 3 attempts, exponential backoff 1-10s ±20% jitter.

`_validate_point_payload()` checks that `point.payload["tenant_id"] == str(ctx.tenant_id)`. Mismatch raises `ValueError` — this prevents a bug in the ingest graph from writing cross-tenant data.

### Step 5: `delete_by_document()` method

```python
async def delete_by_document(
    self,
    ctx: TenantContext,
    qdrant_collection: str,
    document_id: UUID,
) -> int:
    """Delete all Qdrant points for a document within the tenant.

    Args:
        ctx: Tenant context. Ensures deletion is scoped to the tenant.
        qdrant_collection: Physical Qdrant collection name.
        document_id: Document whose chunks to delete.

    Returns:
        Number of points deleted (from Qdrant operation result).

    Raises:
        QdrantUnavailableError: If Qdrant is unreachable after retries.
    """
    delete_filter = build_document_delete_filter(
        tenant_id=str(ctx.tenant_id),
        document_id=str(document_id),
    )

    log.info("retrieval.delete_by_document",
             tenant_id=str(ctx.tenant_id),
             document_id=str(document_id))

    result = await self._client.delete(
        collection_name=qdrant_collection,
        points_selector=FilterSelector(filter=delete_filter),
        wait=True,
    )
    # Qdrant returns operation_id; count is not always available in delete response.
    # Return 0 if count unavailable — DeletionService tracks count via chunks_registry.
    return getattr(result, "result", {}).get("count", 0)
```

### Step 6: `delete_by_tenant()` method

```python
async def delete_by_tenant(
    self,
    ctx: TenantContext,
    qdrant_collection: str,
) -> int:
    """Delete ALL points for a tenant. For GDPR right-to-erasure at tenant offboarding.

    This is a destructive operation. It must only be called by DeletionService
    in the context of a full tenant deletion (Owner role or system-level action).

    Args:
        ctx: Tenant context.
        qdrant_collection: Physical Qdrant collection name.

    Returns:
        Approximate number of points deleted.
    """
    delete_filter = build_tenant_delete_filter(str(ctx.tenant_id))

    log.warning("retrieval.delete_by_tenant",
                tenant_id=str(ctx.tenant_id),
                collection=qdrant_collection)

    result = await self._client.delete(
        collection_name=qdrant_collection,
        points_selector=FilterSelector(filter=delete_filter),
        wait=True,
    )
    return getattr(result, "result", {}).get("count", 0)
```

### Step 7: FastAPI dependency

Create `src/api/dependencies/retrieval.py`:

```python
from qdrant_client import AsyncQdrantClient
from src.core.config import settings
from src.retrieval.service import RetrievalService
from fastapi import Depends, Request

# The AsyncQdrantClient is initialized ONCE in the FastAPI lifespan context
# and stored in app state: app.state.qdrant_client
# Do NOT use @lru_cache on objects that hold async connection pools.
#
# In src/main.py lifespan:
#   app.state.qdrant_client = AsyncQdrantClient(
#       url=settings.qdrant_url, timeout=10.0
#   )
#
# Retrieved via:
#   QdrantClient(url=settings.qdrant_url)

def get_qdrant_client(request: Request) -> AsyncQdrantClient:
    return request.app.state.qdrant_client

def get_retrieval_service(
    client: AsyncQdrantClient = Depends(get_qdrant_client),
) -> RetrievalService:
    return RetrievalService(client=client)

# Usage in router:
# svc: RetrievalService = Depends(get_retrieval_service)
```

### Step 8: Helper — `_merge_filters()`

```python
def _merge_filters(mandatory: Filter, additional: Filter | None) -> Filter:
    """Merge an additional filter into the mandatory tenant+RBAC filter.

    The mandatory filter is never overridden or replaced — only extended.
    Additional conditions are appended to the `must` list.
    """
    if additional is None:
        return mandatory
    merged_must = list(mandatory.must or []) + list(additional.must or [])
    return Filter(must=merged_must)
```

---

## API Contracts

`RetrievalService` is not a REST endpoint. It is a domain service used internally by:

- `src/graphs/query_graph/nodes/node_retrieve.py` — search during query pipeline
- `src/graphs/ingest_graph/nodes/node_upsert.py` — upsert during ingest pipeline
- `src/domain/deletion_service.py` — cascading deletion (TASK-013)
- `src/domain/collection_service.py` — `CollectionService.create_collection()` calls `ensure_collection()` — this is the only legitimate caller outside the retrieval/ingest pipeline

The service interface is the contract:

```python
class RetrievalServiceProtocol(Protocol):
    async def search(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        query_vector: list[float],
        top_k: int,
        score_threshold: float,
        additional_filter: Filter | None,
    ) -> list[RetrievalResult]: ...

    async def upsert_batch(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        points: list[QdrantPoint],
    ) -> None: ...

    async def delete_by_document(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
        document_id: UUID,
    ) -> int: ...

    async def delete_by_tenant(
        self,
        ctx: TenantContext,
        qdrant_collection: str,
    ) -> int: ...

    async def ensure_collection(
        self,
        embedding_model_slug: str,
        vector_size: int,
        distance: str = "Cosine",
    ) -> None: ...
```

---

## Security Checklist

- [ ] `build_mandatory_filter()` is the ONLY place that constructs Qdrant query filters. No other filter construction exists anywhere in the codebase.
- [ ] `EmptyCollectionListError` is raised if `ctx.allowed_collection_ids` is empty — never fall back to a tenant-wide scan.
- [ ] `_validate_point_payload()` in `upsert_batch()` verifies every point's `payload["tenant_id"]` matches `ctx.tenant_id` before sending to Qdrant.
- [ ] `delete_by_tenant()` logs at `WARNING` level and records an audit entry — called only from `DeletionService`.
- [ ] No `qdrant_client` import exists outside `src/retrieval/`. Verified by `tests/security/test_architecture.py`.
- [ ] No module imports `RetrievalService` and then passes `TenantContext(tenant_id=..., allowed_collection_ids=[])` to `search()` — the empty list guard prevents this from silently returning empty results instead of raising.
- [ ] Logs contain only `tenant_id` (UUID), `document_id` (UUID), counts, and metric values. No vector content, no chunk text.
- [ ] `search()` uses `with_payload=True` — the payload including `text` is returned to the caller, but never logged.
- [ ] Tenacity retry decorators are applied to ALL network calls — no raw `self._client.*` calls without retry.

---

## Terms of Use (relevant constraints)

- **This is the only Qdrant client in the codebase.** Any PR that adds an `AsyncQdrantClient` instantiation outside `src/retrieval/` is rejected. This is enforced by `tests/security/test_architecture.py` (import linter using `ast.walk`).
- **Physical Qdrant collection naming** follows the pattern `emb_{embedding_model_slug}`. Collection name = `f"emb_{embedding_model_slug}"` where `embedding_model_slug` is the `models_registry.name` field value, lowercased and with spaces/hyphens replaced by underscores. Callers (ingest node, query node) resolve the collection name and pass it to the service.
- **No result filtering by content** — the service returns raw Qdrant hits; relevance filtering (grading) is done in the query graph `grade_documents` node.
- **Score threshold default** is 0.35 per `docs/architecture.md` §9 retrieval parameters table. The default in `search()` is 0.0 (no cutoff) to allow the caller to apply the pipeline-specific threshold. The query graph node passes the value from `rag_pipelines.prompt_config.score_threshold`.
- **Phase 3 hybrid search**: `reranker.py` is a placeholder. When implemented, `RetrievalService` will call it internally after `_client.search()`. The interface does not change.

---

## Tests

### Unit tests (`tests/unit/retrieval/test_retrieval_service.py`)

Use `AsyncMock` for `AsyncQdrantClient`. No real Qdrant in unit tests.

**Filter invariants (critical security tests):**

| Test | Setup | Assert |
|---|---|---|
| `test_search_always_includes_tenant_filter` | Any `search()` call | `client.search` called with filter containing `tenant_id == ctx.tenant_id` |
| `test_search_always_includes_collection_filter` | Any `search()` call | Filter contains `collection_id in ctx.allowed_collection_ids` |
| `test_search_empty_collections_raises` | `ctx.allowed_collection_ids = []` | Raises `EmptyCollectionListError` before any Qdrant call |
| `test_search_cannot_bypass_tenant_with_additional_filter` | Additional filter with different `tenant_id` condition | Mandatory filter is still present; not replaced |
| `test_upsert_wrong_tenant_in_payload_raises` | Point with `payload["tenant_id"] != ctx.tenant_id` | Raises `ValueError` before any Qdrant call |
| `test_delete_by_document_scoped_to_tenant` | `delete_by_document(ctx, doc_id)` | Qdrant delete called with both `tenant_id` AND `document_id` filters |
| `test_delete_by_tenant_uses_only_tenant_filter` | `delete_by_tenant(ctx)` | Qdrant delete called with only `tenant_id` filter |

**Behavior tests:**

| Test | Setup | Assert |
|---|---|---|
| `test_search_returns_retrieval_results` | Qdrant mock returns 3 hits | Returns list of 3 `RetrievalResult` with correct `score`, `document_id`, `page_number` |
| `test_search_respects_top_k` | `top_k=3` | Qdrant client called with `limit=3` |
| `test_search_passes_score_threshold` | `score_threshold=0.5` | Qdrant client called with `score_threshold=0.5` |
| `test_upsert_batch_sends_all_points` | 50 points | Qdrant `upsert()` called with 50 `PointStruct` objects |
| `test_upsert_uses_wait_true` | Any upsert | Qdrant `upsert(wait=True)` |
| `test_search_retries_on_qdrant_error` | Qdrant mock raises on first call, succeeds on second | Returns results; no exception propagated |
| `test_search_raises_after_max_retries` | Qdrant mock always raises | Raises `QdrantUnavailableError` after 2 attempts |

### Security tests (`tests/security/test_tenant_isolation.py`)

These are `@pytest.mark.tenant_isolation` — blockers for any merge:

```python
@pytest.mark.tenant_isolation
async def test_cross_tenant_search_returns_no_results(
    retrieval_service: RetrievalService,
    qdrant_with_two_tenant_data: ...,
):
    """Tenant A cannot see Tenant B's documents even when querying the same collection."""
    ctx_a = TenantContext(
        tenant_id=TENANT_A_ID,
        allowed_collection_ids=[COLLECTION_A_ID],
    )
    results = await retrieval_service.search(
        ctx=ctx_a,
        qdrant_collection="emb_bge_m3",
        query_vector=TENANT_B_VECTOR,  # vector that matches Tenant B docs
        top_k=8,
    )
    # Even with a perfect-match vector, no Tenant B results returned
    for r in results:
        assert r.payload["tenant_id"] == str(TENANT_A_ID)

@pytest.mark.tenant_isolation
async def test_no_qdrant_client_outside_retrieval_module():
    """Import linter: AsyncQdrantClient must not be imported outside src/retrieval/."""
    import ast, pathlib
    src_root = pathlib.Path("src")
    for py_file in src_root.rglob("*.py"):
        if "retrieval" in py_file.parts:
            continue
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in getattr(node, "names", [])]
                module = getattr(node, "module", "") or ""
                assert "qdrant_client" not in module, \
                    f"{py_file} imports qdrant_client — forbidden outside src/retrieval/"
                assert not any("qdrant_client" in n for n in names), \
                    f"{py_file} imports from qdrant_client — forbidden outside src/retrieval/"
```

### Integration tests (`tests/integration/` — uses testcontainers with real Qdrant)

| Test | Assert |
|---|---|
| `test_upsert_then_search_returns_correct_results` | Upsert 10 points; search returns matching ones with correct scores |
| `test_delete_by_document_removes_only_target_chunks` | Insert chunks for doc_A and doc_B; delete doc_A; search still returns doc_B chunks |
| `test_tenant_isolation_with_real_qdrant` | Insert points for tenant_A and tenant_B; search as tenant_A; no tenant_B results |
| `test_empty_collection_list_never_reaches_qdrant` | `ctx.allowed_collection_ids = []`; assert `qdrant_client.search` never called |

---

## Definition of Done

- [ ] `src/retrieval/service.py` implements all 4 public methods with type annotations.
- [ ] `src/retrieval/filters.py` contains `build_mandatory_filter()` as the single filter construction point.
- [ ] `EmptyCollectionListError` raised synchronously (before any network call) when `allowed_collection_ids` is empty.
- [ ] `_validate_point_payload()` raises `ValueError` on `tenant_id` mismatch in `upsert_batch()`.
- [ ] All `AsyncQdrantClient` calls wrapped with tenacity retry per `docs/architecture.md` §16.
- [ ] `mypy src/retrieval/` passes with no errors.
- [ ] All unit tests pass: `pytest tests/unit/retrieval/ -x -q`.
- [ ] All `@pytest.mark.tenant_isolation` tests in `tests/security/` pass.
- [ ] Integration test with testcontainers Qdrant passes.
- [ ] Import linter test confirms no `qdrant_client` outside `src/retrieval/`.
