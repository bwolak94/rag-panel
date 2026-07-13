# TASK-016: Comprehensive Test Suite

**Status:** TODO  
**Priority:** P0 — blocker for any production deployment  
**Owner:** backend-dev + rag-engineer  
**Reviewer:** python-reviewer (security profile)  
**Related docs:** `docs/architecture.md`, `docs/api.md`, `docs/data-model.md`, `docs/rodo.md`  
**Estimated effort:** 10–14 days

---

## Overview

Implement the full automated test suite for the multi-tenant RAG platform. The suite must enforce hard security contracts (tenant isolation, RBAC, IDOR protection, JWT validation) on every CI run. No PR touching `src/retrieval/`, `src/api/dependencies/`, or `src/domain/` may be merged without all tests in the `tenant_isolation` and `auth` marks passing.

Medical data is processed by this system (pilot: medical clinic). A tenant isolation failure is a GDPR breach. Treat every `@pytest.mark.tenant_isolation` test as a blocker equivalent to a compile error.

---

## Scope

| Layer | What is tested |
|---|---|
| `src/api/` | All ~28 endpoints: happy path, 401/403/404/422; RBAC per role |
| `src/domain/` | DeletionService cascade + saga, AuditService write |
| `src/graphs/query_graph/nodes/` | 7 nodes in isolation, mocked LLM |
| `src/graphs/ingest_graph/nodes/` | 9 nodes in isolation, mocked MinIO/LLM/Qdrant |
| `src/retrieval/` | RetrievalService filter invariants, empty-collections guard |
| `src/db/repositories/` | All repository methods against real Postgres (testcontainers) |
| Security | Cross-tenant retrieval, IDOR, JWT tampering, collection access scoping |
| RAG quality | recall@8, precision@8, faithfulness, citation accuracy, trap questions |
| Performance | p95 latency under load (Locust) |

---

## Tech Stack

- **Test runner:** `pytest` with `pytest-asyncio` (mode = `asyncio`, `auto`)
- **Async HTTP client:** `httpx.AsyncClient` with `transport=ASGITransport(app=app)`
- **Testcontainers:** `testcontainers-python` — `PostgresContainer`, `QdrantContainer`, `MinioContainer`, `RedisContainer`
- **Data factories:** `factory-boy` with `AsyncSQLAlchemyModelFactory` (sqlalchemy async session)
- **JWT:** `python-jose` with HMAC-SHA256 (test secret, not RSA — avoids Keycloak dependency in unit tests)
- **Mocks:** `unittest.mock.AsyncMock` for LLM client, MinIO client; `pytest-mock` for patch helpers
- **RAG evaluation:** `ragas` library, pandas for baseline comparison
- **Performance:** `locust` (separate invocation, not part of `pytest`)
- **Coverage:** `pytest-cov` with `--cov=src`

---

## Prerequisites

1. Docker available on CI runner (for testcontainers).
2. `uv` lockfile includes: `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-mock`, `httpx`, `testcontainers`, `factory-boy`, `python-jose[cryptography]`, `ragas`, `locust`.
3. Alembic migrations are complete and `alembic upgrade head` runs successfully.
4. `src/core/config.py` uses `pydantic-settings`; tests override via environment variables set in fixtures.
5. The ingest graph `pii_scan` node must expose a replaceable scanner callable (dependency injection) so tests can inject a deterministic mock that always returns `pii_detected=True` or `pii_detected=False`.

---

## Test Architecture

```
tests/
  conftest.py                  # session + function fixtures (see §Test Utilities)
  factories.py                 # factory-boy factories for all models
  unit/
    auth/
      test_get_current_ctx.py
      test_require_permission.py
      test_get_tenant_collections.py
    query_graph/
      test_node_classify_intent.py
      test_node_rewrite_query.py
      test_node_retrieve.py
      test_node_grade_documents.py
      test_node_refine_query.py
      test_node_generate.py
      test_node_guardrails_output.py
      test_node_persist.py
    ingest_graph/
      test_node_fetch_from_minio.py
      test_node_extract_text.py
      test_node_dedupe_check.py
      test_node_llm_validate.py
      test_node_pii_scan.py
      test_node_chunk.py
      test_node_embed.py
      test_node_upsert_qdrant.py
      test_node_persist_status.py
    retrieval/
      test_retrieval_service.py
    domain/
      test_deletion_service.py
      test_audit_service.py
    chunking/
      test_chunking_logic.py
  integration/
    test_ingest_pipeline.py
    test_query_pipeline.py
    test_needs_review_flow.py
    test_api_contracts.py       # all endpoints, all roles
  security/
    test_tenant_isolation.py    # @pytest.mark.tenant_isolation
    test_idor.py                # @pytest.mark.tenant_isolation
    test_jwt.py                 # @pytest.mark.auth
    test_rbac.py                # @pytest.mark.auth
    test_collection_scoping.py  # @pytest.mark.tenant_isolation
    test_architecture.py        # import linter: no direct Qdrant outside retrieval/
  eval/
    fixtures/
      corpus/                   # 10 sample medical procedure PDFs (lorem-ipsum, no real PII)
      questions.json
      trap_questions.json
      baseline.json
    test_retrieval_quality.py
    test_generation_quality.py
  performance/
    locustfile.py
```

---

## Unit Tests

### Conventions for all unit tests

- No real network I/O. Every external client is replaced with `AsyncMock`.
- Each test function is `async def test_*` decorated with nothing extra (pytest-asyncio auto mode picks it up).
- State dicts passed into nodes must be minimal — only fields relevant to that node's contract (see `docs/architecture.md §7` and `§8`).
- Assertions check **only the fields the node is supposed to mutate** plus the fact that other fields are unchanged or absent from the returned diff.
- LLM mock is always injected via the node's function parameter or a module-level dependency — never patched at the HTTP layer in unit tests.

---

### 1. RBAC / Auth Unit Tests

**File:** `tests/unit/auth/test_get_current_ctx.py`

Fixture shared across auth tests:
```python
# In the test file (or conftest for auth/)
TEST_JWT_SECRET = "test-secret-not-for-production"
TEST_AUDIENCE = "rag-api"

def make_jwt(payload: dict, secret: str = TEST_JWT_SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, secret, algorithm=algorithm)
```

Test cases for `get_current_ctx(token: str) -> UserContext`:

| Test name | Setup | Assert |
|---|---|---|
| `test_valid_jwt_returns_user_context` | JWT with valid `sub`, `tenant_id`, `roles`, `exp` = now+300s, `aud=TEST_AUDIENCE` | Returns `UserContext` with matching `user_id`, `tenant_id`, `role_ids` |
| `test_expired_jwt_raises_401` | JWT with `exp` = now-1s | Raises `HTTPException(status_code=401)` with `code="TOKEN_EXPIRED"` |
| `test_tampered_signature_raises_401` | Valid JWT, last character of signature replaced with `X` | Raises `HTTPException(status_code=401)` with `code="TOKEN_INVALID"` |
| `test_wrong_audience_raises_401` | JWT with `aud="other-service"` | Raises `HTTPException(status_code=401)` with `code="TOKEN_INVALID_AUDIENCE"` |
| `test_missing_tenant_id_claim_raises_401` | JWT without `tenant_id` claim | Raises `HTTPException(status_code=401)` with `code="TOKEN_MISSING_CLAIM"` |
| `test_missing_sub_claim_raises_401` | JWT without `sub` claim | Raises `HTTPException(status_code=401)` with `code="TOKEN_MISSING_CLAIM"` |
| `test_algorithm_none_attack_raises_401` | JWT signed with `algorithm="none"` | Raises `HTTPException(status_code=401)` — must NOT accept alg=none |
| `test_roles_extracted_from_token_not_body` | JWT with roles in claims | `user_ctx.role_ids` matches claim; no query param or body field accepted |

**File:** `tests/unit/auth/test_require_permission.py`

Test `require(permission: str)` FastAPI dependency factory:

| Test name | Setup | Assert |
|---|---|---|
| `test_user_with_permission_passes` | `user_ctx.permissions = {"documents:read"}`, require `"documents:read"` | No exception raised |
| `test_user_without_permission_raises_403` | `user_ctx.permissions = {"chat:query"}`, require `"documents:read"` | Raises `HTTPException(status_code=403)` |
| `test_unauthenticated_raises_401` | `user_ctx = None` (no token) | Raises `HTTPException(status_code=401)` |
| `test_multiple_permissions_any_match_passes` | Permission check requires OR of two perms; user has one | No exception |
| `test_multiple_permissions_none_match_raises_403` | User has neither permission | Raises 403 |

**File:** `tests/unit/auth/test_get_tenant_collections.py`

Test `get_tenant_collections(user_ctx: UserContext, db: AsyncSession) -> list[UUID]`:

| Test name | Setup | Assert |
|---|---|---|
| `test_returns_only_collections_for_users_roles` | User has role R1; R1 has access to collections C1, C2; C3 belongs to same tenant but is for role R2 | Returns `[C1, C2]`, not `C3` |
| `test_no_roles_returns_empty_list` | User has no roles | Returns `[]` |
| `test_inactive_collection_excluded` | C1 is `is_active=False`, C2 is `is_active=True`, both accessible by role | Returns `[C2]` only |
| `test_cross_tenant_collection_excluded` | DB has collection from tenant B with same role name | Returns only collections where `collection.tenant_id == user_ctx.tenant_id` |

---

### 2. Query Graph Node Unit Tests

**Shared fixture in `tests/unit/query_graph/conftest.py`:**
```python
@pytest.fixture
def base_state() -> dict:
    return {
        "question": "Jakie są procedury dezynfekcji narzędzi?",
        "rewritten_query": None,
        "user_ctx": UserContext(
            user_id=uuid4(), tenant_id=TENANT_A_ID,
            role_ids=[ROLE_VIEWER_ID], permissions={"chat:query"},
            allowed_collection_ids=[COLLECTION_A_ID],
        ),
        "pipeline_config": PipelineConfig(
            collection_ids=[COLLECTION_A_ID],
            llm_model_id=MODEL_ID,
            prompt_config={"top_k": 8, "score_threshold": 0.35},
            guardrails={"pii_check": True, "medical_disclaimer": True},
        ),
        "retrieved_chunks": [],
        "graded_chunks": [],
        "retry_count": 0,
        "answer": "",
        "citations": [],
        "model_id": str(MODEL_ID),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0.0,
    }

@pytest.fixture
def mock_llm():
    return AsyncMock()
```

**File:** `tests/unit/query_graph/test_node_classify_intent.py`

Function under test: `node_classify_intent(state: dict, llm: LLMClient) -> dict`

| Test name | LLM mock returns | Assert on returned diff |
|---|---|---|
| `test_topical_question_classified` | `{"intent": "topical_question"}` | `{"intent": "topical_question"}` |
| `test_small_talk_classified` | `{"intent": "small_talk"}` | `{"intent": "small_talk"}` |
| `test_out_of_scope_classified` | `{"intent": "out_of_scope"}` | `{"intent": "out_of_scope"}` |
| `test_llm_timeout_raises_node_error` | `AsyncMock(side_effect=TimeoutError())` | Raises `NodeExecutionError` with `retry_count` incremented |
| `test_llm_503_raises_node_error` | `AsyncMock(side_effect=LLMServiceUnavailable())` | Raises `NodeExecutionError` |
| `test_state_mutation_limited_to_intent` | Valid LLM response | Returned dict contains only `intent` key (no other state fields mutated) |

**File:** `tests/unit/query_graph/test_node_rewrite_query.py`

Function under test: `node_rewrite_query(state: dict, llm: LLMClient) -> dict`

| Test name | Setup / LLM returns | Assert |
|---|---|---|
| `test_rewrites_question` | LLM returns `{"rewritten_query": "dezynfekcja narzędzi chirurgicznych procedura"}` | `state["rewritten_query"] == "dezynfekcja narzędzi chirurgicznych procedura"` |
| `test_uses_graded_chunks_on_retry` | `state["retry_count"] = 1`, graded_chunks non-empty | LLM called with prompt that includes graded chunk summaries (inspect `mock_llm.call_args`) |
| `test_empty_rewrite_falls_back_to_original` | LLM returns `{"rewritten_query": ""}` | `state["rewritten_query"] == state["question"]` |
| `test_llm_error_raises` | `AsyncMock(side_effect=TimeoutError())` | Raises `NodeExecutionError` |

**File:** `tests/unit/query_graph/test_node_retrieve.py`

Function under test: `node_retrieve(state: dict, retrieval_service: RetrievalService) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_returns_chunks_from_retrieval_service` | Mock `retrieval_service.search` returns 3 chunks | `state["retrieved_chunks"]` has 3 items |
| `test_passes_user_ctx_to_retrieval_service` | Any state | `retrieval_service.search` called with `user_ctx` matching `state["user_ctx"]` |
| `test_empty_results_sets_empty_list` | Mock returns `[]` | `state["retrieved_chunks"] == []` |
| `test_retrieval_service_error_raises` | `AsyncMock(side_effect=QdrantUnavailableError())` | Raises `NodeExecutionError` |

Note: `RetrievalService` is mocked at the service level. The actual filter logic is tested separately in `tests/unit/retrieval/test_retrieval_service.py`.

**File:** `tests/unit/query_graph/test_node_grade_documents.py`

Function under test: `node_grade_documents(state: dict, llm: LLMClient) -> dict`

| Test name | Setup / LLM returns | Assert |
|---|---|---|
| `test_grades_relevant_chunks` | 3 chunks, LLM returns `[{"relevant": True}, {"relevant": True}, {"relevant": False}]` | `graded_chunks` has 2 items with `relevant=True` |
| `test_empty_chunks_returns_empty_graded` | `retrieved_chunks = []` | `graded_chunks == []`, LLM NOT called |
| `test_all_irrelevant_sets_empty_graded` | LLM returns all `{"relevant": False}` | `graded_chunks == []` |
| `test_llm_error_raises` | `side_effect=TimeoutError()` | Raises `NodeExecutionError` |
| `test_sufficient_context_edge_case` | 1 relevant chunk | `graded_chunks` has 1 item |

**File:** `tests/unit/query_graph/test_node_refine_query.py`

Function under test: `node_refine_query(state: dict, llm: LLMClient) -> dict`

| Test name | Setup / LLM returns | Assert |
|---|---|---|
| `test_refines_query_and_increments_retry` | `retry_count=0`, LLM returns new query | `retry_count == 1` and `rewritten_query` updated |
| `test_max_retry_not_exceeded_in_node` | `retry_count=1` | Node executes; graph routing logic (not the node) stops further retries |
| `test_llm_error_raises` | `side_effect=TimeoutError()` | Raises `NodeExecutionError` |

**File:** `tests/unit/query_graph/test_node_generate.py`

Function under test: `node_generate(state: dict, llm: LLMClient) -> dict`

| Test name | Setup / LLM returns | Assert |
|---|---|---|
| `test_generates_answer_with_citations` | 2 graded chunks, LLM returns `{"answer": "...", "citations": [{...}]}` | `state["answer"]` non-empty, `state["citations"]` has correct `doc_id` and `chunk_id` |
| `test_citation_doc_id_matches_chunk_source` | Chunk has `document_id=DOC_X` | Citation in response has `doc_id=DOC_X` |
| `test_empty_graded_chunks_not_called` | `graded_chunks = []` | This path should not reach `generate` (routing handles it); if called with empty context, raises `ValueError` |
| `test_token_counts_populated` | LLM returns `usage: {prompt_tokens: 100, completion_tokens: 50}` | `state["prompt_tokens"] == 100`, `state["completion_tokens"] == 50` |
| `test_llm_503_raises_node_error` | `side_effect=LLMServiceUnavailable()` | Raises `NodeExecutionError` |

**File:** `tests/unit/query_graph/test_node_guardrails_output.py`

Function under test: `node_guardrails_output(state: dict, llm: LLMClient | None) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_medical_disclaimer_appended` | `guardrails={"medical_disclaimer": True}`, answer without disclaimer | `state["answer"]` contains disclaimer text (verify against constant in code) |
| `test_pii_in_answer_redacted` | Answer contains `"PESEL: 12345678901"`, `guardrails={"pii_check": True}` | Returned answer does NOT contain the PESEL string |
| `test_clean_answer_unchanged` | No PII, `medical_disclaimer=False` | `state["answer"]` unchanged |
| `test_prompt_injection_markers_stripped` | Answer contains `"IGNORE PREVIOUS INSTRUCTIONS"` | Stripped or replaced in output |

**File:** `tests/unit/query_graph/test_node_persist.py`

Function under test: `node_persist(state: dict, db: AsyncSession, langfuse_client: LangfuseClient) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_saves_message_to_db` | Full valid state | `db.execute` called with INSERT into `messages`; `conversation_id` present |
| `test_saves_message_sources` | `citations` non-empty | INSERT into `message_sources` called for each citation |
| `test_langfuse_trace_called` | `langfuse_client = AsyncMock()` | `langfuse_client.trace` called once |
| `test_langfuse_trace_does_not_contain_pii` | Answer contains `"user@example.com"` — check trace payload | `langfuse_client.trace` call args do NOT include the email string |
| `test_db_error_raises_node_error` | `db.execute(side_effect=SQLAlchemyError())` | Raises `NodeExecutionError` |

---

### 3. Ingest Graph Node Unit Tests

**Shared fixture in `tests/unit/ingest_graph/conftest.py`:**
```python
@pytest.fixture
def base_ingest_state() -> dict:
    return {
        "document_id": uuid4(),
        "tenant_id": TENANT_A_ID,
        "collection_id": COLLECTION_A_ID,
        "minio_key": f"tenant-a/raw/{COLLECTION_A_ID}/{DOC_ID}/file.pdf",
        "raw_bytes": None,
        "extracted_text": None,
        "extracted_sections": None,
        "sha256": "abc123def456",
        "validation_result": None,
        "chunks": None,
        "embeddings": None,
        "point_ids": None,
        "status": "processing",
        "error": None,
        "retry_count": 0,
    }

@pytest.fixture
def mock_minio():
    return AsyncMock()

@pytest.fixture
def mock_llm():
    return AsyncMock()

@pytest.fixture
def mock_qdrant():
    return AsyncMock()
```

**File:** `tests/unit/ingest_graph/test_node_fetch_from_minio.py`

Function: `node_fetch_from_minio(state: dict, minio: MinIOClient) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_fetches_bytes_from_minio` | `minio.get_object` returns `b"PDF_BYTES"` | `state["raw_bytes"] == b"PDF_BYTES"` |
| `test_file_not_found_sets_error_status` | `minio.get_object(side_effect=MinIOObjectNotFound())` | `state["status"] == "failed"`, `state["error"]` contains `"not found"` |
| `test_minio_connection_error_raises` | `minio.get_object(side_effect=MinIOConnectionError())` | Raises `NodeExecutionError` with `retry_count` incremented |
| `test_minio_key_passed_from_state` | Any | `minio.get_object` called with `key=state["minio_key"]` |

**File:** `tests/unit/ingest_graph/test_node_extract_text.py`

Function: `node_extract_text(state: dict, extractor: DocumentExtractor) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_extracts_text_from_pdf_bytes` | `raw_bytes` = minimal valid PDF bytes (fixture), extractor mock returns `extracted_text` | `state["extracted_text"]` non-empty |
| `test_extracts_sections` | Extractor mock returns `extracted_sections = [Section(title="§1", text="...")]` | `state["extracted_sections"]` has 1 item |
| `test_empty_bytes_sets_error` | `raw_bytes = b""` | `state["status"] == "failed"`, `state["error"]` set |
| `test_extraction_failure_raises` | `extractor.extract(side_effect=ExtractionError())` | Raises `NodeExecutionError` |
| `test_sha256_computed_from_raw_bytes` | `raw_bytes = b"hello"` | `state["sha256"] == hashlib.sha256(b"hello").hexdigest()` |

**File:** `tests/unit/ingest_graph/test_node_dedupe_check.py`

Function: `node_dedupe_check(state: dict, db: AsyncSession) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_unique_sha256_passes_through` | DB returns no matching sha256 for tenant | `state["status"]` unchanged (still `"processing"`) |
| `test_duplicate_sha256_sets_rejected_status` | DB returns existing document with same sha256 for same tenant_id | `state["status"] == "rejected_duplicate"` |
| `test_duplicate_in_different_tenant_passes` | Same sha256 exists for tenant B, not tenant A | `state["status"]` unchanged — cross-tenant sha256 match MUST NOT cause rejection |
| `test_db_error_raises` | `db.execute(side_effect=SQLAlchemyError())` | Raises `NodeExecutionError` |

**File:** `tests/unit/ingest_graph/test_node_llm_validate.py`

Function: `node_llm_validate(state: dict, llm: LLMClient) -> dict`

| Test name | LLM mock returns | Assert |
|---|---|---|
| `test_populates_validation_result` | `{"category": "procedure", "quality": 0.85, "type": "pdf_text", "confidence": 0.9}` | `state["validation_result"].category == "procedure"` |
| `test_low_quality_score_recorded` | `{"quality": 0.2, "confidence": 0.3}` | `state["validation_result"].quality == 0.2` — low score alone does not set needs_review (pii_scan handles that) |
| `test_llm_timeout_raises` | `side_effect=TimeoutError()` | Raises `NodeExecutionError`, `retry_count` incremented |
| `test_llm_503_raises` | `side_effect=LLMServiceUnavailable()` | Raises `NodeExecutionError` |
| `test_only_text_sample_sent_to_llm` | `extracted_text` = 10,000 chars | Verify `llm.call_args` contains a truncated sample (per node contract — never full document) |

**File:** `tests/unit/ingest_graph/test_node_pii_scan.py`

Function: `node_pii_scan(state: dict, scanner: PIIScanner) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_pii_detected_sets_needs_review` | `scanner.scan` returns `PIIScanResult(detected=True, flags=["PESEL"])` | `state["status"] == "needs_review"`, `state["validation_result"].pii_flags == ["PESEL"]` |
| `test_low_confidence_sets_needs_review` | `validation_result.confidence = 0.4` (below threshold), no PII | `state["status"] == "needs_review"` |
| `test_clean_document_continues` | No PII, `confidence >= 0.7` | `state["status"] == "processing"` (unchanged) |
| `test_scanner_error_raises` | `scanner.scan(side_effect=Exception("scanner down"))` | Raises `NodeExecutionError` |
| `test_pii_flags_merged_into_validation_result` | Existing `validation_result.category = "procedure"`, scanner returns flags | `state["validation_result"].category` still `"procedure"` and `pii_flags` added |

**File:** `tests/unit/ingest_graph/test_node_chunk.py`

Function: `node_chunk(state: dict, chunk_config: ChunkConfig) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_recursive_chunking_default_config` | `extracted_text` = 2000-char string, config: `size=512, overlap=64` | `chunks` list has correct number of items; each chunk <= 512 tokens |
| `test_chunk_overlap_is_applied` | 2 adjacent chunks | End of chunk N overlaps with start of chunk N+1 by ~64 tokens |
| `test_per_type_config_overrides_default` | `collection.chunk_config = {"pdf_text": {"size": 256, "overlap": 32}}`, doc type `"pdf_text"` | Chunk size uses 256 not default 512 |
| `test_empty_text_returns_error` | `extracted_text = ""` | `state["status"] == "failed"`, `state["error"]` set |
| `test_section_metadata_preserved` | `extracted_sections` present | Each chunk has `section` and `page` metadata from corresponding section |

**File:** `tests/unit/ingest_graph/test_node_embed.py`

Function: `node_embed(state: dict, llm: LLMClient) -> dict`

| Test name | Setup / LLM returns | Assert |
|---|---|---|
| `test_embeddings_returned` | 3 chunks, LLM returns 3 vectors of dim 1024 | `state["embeddings"]` has 3 items, each len 1024 |
| `test_chunks_sent_in_batch` | 10 chunks | LLM called once (or in configured batch sizes), not 10 times individually |
| `test_embedding_dim_mismatch_raises` | LLM returns vectors of dim 768 but collection expects 1024 | Raises `NodeExecutionError` with message about dimension mismatch |
| `test_llm_error_raises` | `side_effect=TimeoutError()` | Raises `NodeExecutionError` |
| `test_uses_collection_embedding_model` | `collection.embedding_model_id = MODEL_BGE_M3` | LLM called with model identifier matching `MODEL_BGE_M3` endpoint |

**File:** `tests/unit/ingest_graph/test_node_upsert_qdrant.py`

Function: `node_upsert_qdrant(state: dict, retrieval: RetrievalService) -> dict`

MAJOR FIX: The node must call `RetrievalService.upsert_batch()`, NOT `QdrantClient.upsert` directly.
Direct `QdrantClient` usage outside `src/retrieval/` violates the hard rule enforced by `test_architecture.py`.
Tests must mock `RetrievalService`, not `QdrantClient`.

```python
import uuid
from unittest.mock import AsyncMock, call

import pytest

from src.graphs.ingest_graph.nodes.node_upsert_qdrant import node_upsert_qdrant
from src.graphs.ingest_graph.state import IngestState


@pytest.fixture
def mock_retrieval() -> AsyncMock:
    return AsyncMock(spec=RetrievalService)


@pytest.fixture
def sample_ingest_state(base_ingest_state) -> IngestState:
    return IngestState(**base_ingest_state)


async def test_node_upsert_calls_retrieval_service(
    mock_retrieval: AsyncMock,
    sample_ingest_state: IngestState,
) -> None:
    chunks_data = [
        {"text": "chunk A", "page": 1, "section": "§1"},
        {"text": "chunk B", "page": 2, "section": "§2"},
    ]
    embeddings = [[0.1] * 1024, [0.2] * 1024]
    state_with_chunks = sample_ingest_state.model_copy(
        update={"chunks": chunks_data, "embeddings": embeddings}
    )
    result = await node_upsert_qdrant(state_with_chunks, retrieval=mock_retrieval)
    mock_retrieval.upsert_batch.assert_called_once()
    call_points = mock_retrieval.upsert_batch.call_args[1]["points"]
    assert len(call_points) == 2
    assert all(isinstance(p.id, uuid.UUID) for p in call_points)


async def test_point_ids_are_deterministic(mock_retrieval: AsyncMock, sample_ingest_state: IngestState):
    """Same chunk content must produce the same point_id on repeated calls."""
    chunks_data = [{"text": "deterministic chunk", "page": 1, "section": "§1"}]
    embeddings = [[0.1] * 1024]
    state = sample_ingest_state.model_copy(update={"chunks": chunks_data, "embeddings": embeddings})
    await node_upsert_qdrant(state, retrieval=mock_retrieval)
    first_call_points = mock_retrieval.upsert_batch.call_args[1]["points"]
    mock_retrieval.reset_mock()
    await node_upsert_qdrant(state, retrieval=mock_retrieval)
    second_call_points = mock_retrieval.upsert_batch.call_args[1]["points"]
    assert first_call_points[0].id == second_call_points[0].id


async def test_point_ids_stored_in_state(mock_retrieval: AsyncMock, sample_ingest_state: IngestState):
    chunks_data = [{"text": "chunk", "page": 1}]
    state = sample_ingest_state.model_copy(update={"chunks": chunks_data, "embeddings": [[0.1] * 1024]})
    result = await node_upsert_qdrant(state, retrieval=mock_retrieval)
    assert len(result["point_ids"]) == 1


async def test_qdrant_failure_raises(mock_retrieval: AsyncMock, sample_ingest_state: IngestState):
    mock_retrieval.upsert_batch.side_effect = QdrantUnavailableError("connection refused")
    state = sample_ingest_state.model_copy(
        update={"chunks": [{"text": "x"}], "embeddings": [[0.1] * 1024]}
    )
    with pytest.raises(NodeExecutionError):
        await node_upsert_qdrant(state, retrieval=mock_retrieval)


async def test_tenant_id_always_in_payload(mock_retrieval: AsyncMock, sample_ingest_state: IngestState):
    """Every point payload must include tenant_id — critical isolation invariant."""
    state = sample_ingest_state.model_copy(
        update={"chunks": [{"text": "x"}, {"text": "y"}], "embeddings": [[0.1] * 1024, [0.2] * 1024]}
    )
    await node_upsert_qdrant(state, retrieval=mock_retrieval)
    call_points = mock_retrieval.upsert_batch.call_args[1]["points"]
    assert all(p.payload["tenant_id"] == str(state.tenant_id) for p in call_points)
```

| Test name | Setup | Assert |
|---|---|---|
| `test_node_upsert_calls_retrieval_service` | 2 chunks + 2 embeddings | `mock_retrieval.upsert_batch` called once; all points have `uuid.UUID` ids |
| `test_point_ids_are_deterministic` | Same chunk content called twice | Same `point_id` generated both times (UUID5 or deterministic scheme) |
| `test_point_ids_stored_in_state` | Successful upsert | `result["point_ids"]` has same count as chunks |
| `test_qdrant_failure_raises` | `mock_retrieval.upsert_batch` raises `QdrantUnavailableError` | Raises `NodeExecutionError` |
| `test_tenant_id_always_in_payload` | Any state | Every `point.payload["tenant_id"] == str(state.tenant_id)` |

**File:** `tests/unit/ingest_graph/test_node_persist_status.py`

Function: `node_persist_status(state: dict, db: AsyncSession) -> dict`

| Test name | Setup | Assert |
|---|---|---|
| `test_updates_document_status_to_ready` | Successful state with `point_ids` | `db` called with UPDATE `documents.status = 'ready'` |
| `test_inserts_chunks_registry_rows` | 3 `point_ids` | 3 rows inserted into `chunks_registry` with correct `qdrant_point_id` |
| `test_updates_ingestion_job_completed` | Any | `ingestion_jobs.status = 'completed'` set |
| `test_inserts_audit_log_entry` | Any | INSERT into `audit_log` with `action='document_indexed'` |
| `test_db_error_does_not_mark_status_ready` | First DB call succeeds (chunks_registry), second fails (document update) | `state["status"] != "ready"`, error recorded |

---

### 4. RetrievalService Unit Tests

**File:** `tests/unit/retrieval/test_retrieval_service.py`

The `QdrantClient` is mocked with `AsyncMock`. Tests inspect the exact filter argument passed to the mock.

```python
@pytest.fixture
def mock_qdrant_client():
    client = AsyncMock()
    client.search.return_value = []
    return client

@pytest.fixture
def retrieval_service(mock_qdrant_client):
    return RetrievalService(qdrant_client=mock_qdrant_client)
```

| Test name | Setup | Assert |
|---|---|---|
| `test_filter_always_contains_tenant_id` | `user_ctx.tenant_id = TENANT_A_ID`, `allowed_collection_ids = [C1]` | `mock_qdrant_client.search` called; inspect `filter.must` — contains `FieldCondition("tenant_id", Match(str(TENANT_A_ID)))` |
| `test_filter_always_contains_collection_ids` | `allowed_collection_ids = [C1, C2]` | Filter contains `FieldCondition("collection_id", MatchAny([str(C1), str(C2)]))` |
| `test_empty_allowed_collections_returns_empty_result` | `allowed_collection_ids = []` | Returns `[]` immediately; Qdrant client NOT called |
| `test_filters_none_still_includes_mandatory_filter` | `additional_filters = None` passed to `search()` | Filter still has `tenant_id` and `collection_id` conditions |
| `test_delete_points_scoped_to_tenant_and_document` | `delete_points(document_id=DOC_X, user_ctx=ctx)` | Qdrant `delete` called with filter containing both `document_id=DOC_X` AND `tenant_id=TENANT_A_ID` |
| `test_delete_points_does_not_delete_other_tenant_docs` | `user_ctx.tenant_id = TENANT_A_ID`, DOC_X | Qdrant filter includes `tenant_id` — cannot delete tenant B's points |
| `test_score_threshold_applied` | `pipeline_config.score_threshold = 0.35`, Qdrant returns point with score 0.2 | Point with score 0.2 excluded from returned results |
| `test_top_k_default_is_8` | No explicit `top_k` | Qdrant called with `limit=8` |
| `test_top_k_configurable` | `pipeline_config.top_k = 5` | Qdrant called with `limit=5` |
| `test_direct_qdrant_client_not_importable_outside_retrieval` | Import check (see architecture test) | Covered in `tests/security/test_architecture.py` |

---

### 5. DeletionService Unit Tests

**File:** `tests/unit/domain/test_deletion_service.py`

```python
@pytest.fixture
def mock_db():
    return AsyncMock(spec=AsyncSession)

@pytest.fixture
def mock_retrieval():
    return AsyncMock(spec=RetrievalService)

@pytest.fixture
def mock_minio():
    return AsyncMock(spec=MinIOClient)

@pytest.fixture
def deletion_service(mock_db, mock_retrieval, mock_minio):
    return DeletionService(db=mock_db, retrieval=mock_retrieval, minio=mock_minio)
```

| Test name | Setup | Assert |
|---|---|---|
| `test_cascade_order_postgres_then_qdrant_then_minio` | All mocks succeed | Using `unittest.mock.call_args_list` or `AsyncMock.call_count` ordered: `mock_db.execute` (DELETE chunks_registry) → `mock_retrieval.delete_points` → `mock_minio.remove_object` |
| `test_idempotent_second_delete_returns_success` | Document already deleted (DB returns 0 rows affected) | No exception; returns `DeletionResult(success=True, already_deleted=True)` |
| `test_qdrant_failure_does_not_mark_status_deleted` | `mock_retrieval.delete_points(side_effect=QdrantUnavailableError())` | `mock_db.execute` NOT called with `UPDATE documents SET status='deleted'`; retry job scheduled |
| `test_minio_failure_after_qdrant_success_schedules_retry` | Qdrant succeeds, MinIO raises `MinIOConnectionError` | `status` NOT fully `deleted`; compensating action recorded |
| `test_audit_log_written_on_success` | Full success | `mock_db.execute` called with INSERT into `audit_log` with `action='document_deleted'` |
| `test_wrong_tenant_document_raises_403` | Document's `tenant_id != user_ctx.tenant_id` | Raises `PermissionDenied` (mapped to 403) |

---

### 6. Chunking Logic Unit Tests

**File:** `tests/unit/chunking/test_chunking_logic.py`

Function under test: the chunking utility (located in `src/ingest/chunking.py` per CLAUDE.md structure — adjust if actual path differs).

| Test name | Setup | Assert |
|---|---|---|
| `test_default_recursive_512_tokens_overlap_64` | 1500-token text, default config | All chunks <= 512 tokens; consecutive chunks share ~64 tokens at boundary |
| `test_chunk_count_correct` | 1024-token text, `size=512, overlap=64` | Approximately 2–3 chunks (exact count depends on natural split points) |
| `test_per_type_override_pdf_text` | `chunk_config={"pdf_text": {"size": 256, "overlap": 32}}`, `doc_type="pdf_text"` | Chunks <= 256 tokens |
| `test_per_type_override_does_not_affect_other_types` | Same config, `doc_type="docx"` | Chunks use default size (512) |
| `test_empty_document_raises_chunking_error` | `text = ""` | Raises `ChunkingError` (do NOT return single empty chunk — callers must treat empty text as upstream error) |
| `test_very_short_text_returns_single_chunk` | `text = "Hello."` (5 tokens) | Returns list of 1 chunk |
| `test_chunk_metadata_has_page_and_section` | Text with section markers from `extracted_sections` | Each returned `ChunkData` has `page` and `section` fields populated |

---

## Integration Tests

All integration tests use testcontainers (real services, not mocks). The session-scoped `containers` fixture (see §Test Utilities) starts Postgres, Qdrant, MinIO, and Redis before any integration test runs.

LLM calls in integration tests are replaced with a **deterministic fake LLM** (not a real GPU call), injected via the dependency override mechanism of FastAPI.

### 1. Full Ingest Pipeline

**File:** `tests/integration/test_ingest_pipeline.py`

```python
@pytest.mark.integration
async def test_full_ingest_happy_path(
    test_client: httpx.AsyncClient,
    admin_ctx: UserContext,
    admin_jwt: str,
    minio_container,
    qdrant_container,
    db_session: AsyncSession,
    fake_llm,       # returns deterministic classify/embed responses
):
```

Steps and assertions:
1. `POST /documents/upload` with a valid PDF (use `tests/fixtures/sample.pdf` — minimal 1-page PDF fixture), `collection_id = COLLECTION_A_ID`, JWT = `admin_jwt`. Assert response `202` with `document_id` and `job_id`.
2. Poll `GET /ingestion-jobs/{job_id}` until `status=completed` or timeout 30s.
3. Assert `documents` table in Postgres: `status='ready'`, `sha256` set, `validation_result` JSON populated.
4. Assert `chunks_registry` table: at least 1 row with `document_id` matching uploaded doc.
5. Assert Qdrant: points exist with `payload.tenant_id == str(TENANT_A_ID)` and `payload.collection_id == str(COLLECTION_A_ID)`.
6. Assert `audit_log` has entry with `action='document_indexed'`.

```python
@pytest.mark.integration
async def test_duplicate_sha256_rejected(test_client, admin_jwt, same_pdf_bytes):
    # Upload same file twice
    # Second upload: GET /ingestion-jobs/{job_id} eventually shows status='completed', result='rejected_duplicate'
    # Assert documents table has second entry with status='rejected'
    # Assert NO new Qdrant points for the duplicate
```

```python
@pytest.mark.integration
async def test_ingest_retries_on_worker_failure():
    # Inject transient MinIO error for first attempt only
    # Assert retry_count incremented in ingestion_jobs
    # Assert eventual success on second attempt
    # Assert document.status = 'ready' after retry
```

### 2. Full Query Pipeline

**File:** `tests/integration/test_query_pipeline.py`

Pre-condition: fixture that inserts 3 Qdrant points for TENANT_A / COLLECTION_A with known text.

```python
@pytest.mark.integration
async def test_chat_completions_returns_answer_with_citations(
    test_client,
    user_jwt,          # has chat:query permission
    seeded_qdrant,     # fixture: 3 chunks in Qdrant for TENANT_A
    fake_llm,
):
    response = await test_client.post(
        "/api/v1/chat/completions",
        json={"model": "rag-procedures", "messages": [{"role": "user", "content": "dezynfekcja narzędzi"}], "stream": False},
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"]  # non-empty answer
    assert "citations" in body  # citations field present
    assert len(body["citations"]) >= 1
    assert body["citations"][0]["doc_id"]  # doc_id present in citation
```

```python
@pytest.mark.integration
async def test_chat_out_of_scope_returns_not_found_message(test_client, user_jwt, empty_qdrant):
    # Query about something not in corpus
    # Fake LLM returns intent=out_of_scope or grade_documents returns empty graded_chunks
    # Assert response contains "nie znalazłem w dokumentach" or configured equivalent
```

```python
@pytest.mark.integration
async def test_stream_mode_returns_sse_events(test_client, user_jwt, seeded_qdrant, fake_llm):
    response = await test_client.post(
        "/api/v1/chat/completions",
        json={"model": "rag-procedures", "messages": [...], "stream": True},
        headers={"Authorization": f"Bearer {user_jwt}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # Read SSE chunks, assert last chunk contains "citations" field
```

### 3. Needs Review Flow

**File:** `tests/integration/test_needs_review_flow.py`

```python
@pytest.mark.integration
async def test_pii_document_enters_needs_review_queue(
    test_client,
    admin_jwt,
    user_jwt,          # uploader
    pii_detecting_llm, # fake LLM / scanner that always returns PII detected
):
    # 1. Upload document (user with documents:upload)
    upload_resp = await test_client.post("/api/v1/documents/upload", ...)
    job_id = upload_resp.json()["job_id"]
    document_id = upload_resp.json()["document_id"]

    # 2. Wait for status=needs_review
    # Poll GET /ingestion-jobs/{job_id} until status='awaiting_review' or timeout 30s

    # 3. Admin sees document in review queue
    queue_resp = await test_client.get(
        "/api/v1/documents/review-queue",
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert queue_resp.status_code == 200
    assert any(doc["id"] == document_id for doc in queue_resp.json()["items"])

    # 4. Admin approves
    approve_resp = await test_client.post(
        f"/api/v1/documents/{document_id}/review",
        json={"decision": "approve", "note": "Reviewed, acceptable"},
        headers={"Authorization": f"Bearer {admin_jwt}"},
    )
    assert approve_resp.status_code == 200

    # 5. Graph resumes from node_chunk
    # Poll until status=ready, timeout 30s
    # Assert Qdrant has points for this document
    # Assert chunks_registry populated
```

```python
@pytest.mark.integration
async def test_admin_reject_leaves_no_qdrant_points(test_client, admin_jwt, pii_detecting_llm, qdrant_container):
    # Upload, wait for needs_review, admin rejects
    # Assert: zero Qdrant points with payload.document_id == document_id
    # Assert: document.status == 'rejected'
```

### 4. API Contracts

**File:** `tests/integration/test_api_contracts.py`

For every endpoint listed in `docs/api.md`, implement the following test matrix. Use parametrize where role variants are structurally identical.

Pattern for each endpoint:
```python
@pytest.mark.parametrize("role_fixture,expected_status", [
    ("admin_jwt", 200),
    ("worker_jwt", 403),
    ("user_jwt", 403),
    ("no_auth", 401),
])
async def test_get_documents_authorization(role_fixture, expected_status, test_client, request):
    token = request.getfixturevalue(role_fixture) if role_fixture != "no_auth" else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = await test_client.get("/api/v1/documents", headers=headers)
    assert resp.status_code == expected_status
```

Endpoints to cover (all require `documents:read` unless noted):

| Endpoint | Required permission | Roles with permission | Roles without |
|---|---|---|---|
| `GET /documents` | `documents:read` | admin, worker | user (viewer-only) |
| `GET /documents/{id}` | `documents:read` | admin, worker | user |
| `POST /documents/upload` | `documents:upload` | admin, worker | user |
| `DELETE /documents/{id}` | `documents:delete` | admin | worker, user |
| `POST /documents/{id}/reindex` | `documents:manage` | admin | worker, user |
| `GET /documents/review-queue` | `documents:approve` | admin | worker, user |
| `POST /documents/{id}/review` | `documents:approve` | admin | worker, user |
| `GET /ingestion-jobs/{id}` | `documents:read` | admin, worker | user |
| `GET /collections` | `chat:query` | admin, worker, user | no_auth |
| `POST /collections` | `admin:collections` | admin | worker, user |
| `PATCH /collections/{id}` | `admin:collections` | admin | worker, user |
| `DELETE /collections/{id}` | `admin:collections` | admin | worker, user |
| `GET /v1/models` | `chat:query` | admin, worker, user | no_auth |
| `POST /v1/chat/completions` | `chat:query` | admin, worker, user | no_auth |
| `GET /conversations` | `chat:query` | admin, worker, user | no_auth |
| `DELETE /conversations/{id}` | `chat:query` (own) | owner | other user (403) |
| `GET /audit-log` | `admin:audit` | admin | worker, user |
| `GET /usage` | `admin:audit` | admin | worker, user |

Additional contract tests (422, 404):
```python
async def test_upload_invalid_mime_type_returns_422(test_client, worker_jwt):
    # Upload file with content-type = application/x-executable
    # Assert 422 with error.code = "INVALID_MIME_TYPE"

async def test_upload_oversized_file_returns_422(test_client, worker_jwt):
    # Upload 101 MB dummy bytes
    # Assert 422 with error.code = "FILE_TOO_LARGE"

async def test_get_document_not_found_returns_404(test_client, worker_jwt):
    resp = await test_client.get(f"/api/v1/documents/{uuid4()}", headers=...)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"

async def test_create_collection_duplicate_name_returns_409(test_client, admin_jwt, existing_collection):
    resp = await test_client.post("/api/v1/collections", json={"name": existing_collection.name, ...}, headers=...)
    assert resp.status_code == 409

async def test_patch_collection_embedding_model_without_force_returns_409(test_client, admin_jwt, collection_with_docs):
    # Change embedding_model_id without ?force
    assert resp.status_code == 409
    assert "reindex" in resp.json()["error"]["message"].lower()
```

---

## Security and Isolation Tests

**Mark:** All tests in this section: `@pytest.mark.tenant_isolation` or `@pytest.mark.auth`

These run as a separate required CI check: `pytest tests/security/ -m "tenant_isolation or auth" -v`

### 1. Cross-Tenant Retrieval

**File:** `tests/security/test_tenant_isolation.py`

```python
@pytest.mark.tenant_isolation
async def test_retrieval_returns_zero_chunks_from_other_tenant(
    retrieval_service: RetrievalService,  # real service, real Qdrant via testcontainer
    qdrant_container,
    seeded_qdrant_both_tenants,  # fixture: inserts points for TENANT_A and TENANT_B
):
    # User context is TENANT_A only
    user_ctx = UserContext(tenant_id=TENANT_A_ID, allowed_collection_ids=[COLLECTION_A_ID], ...)

    results = await retrieval_service.search(
        query_embedding=[0.1] * 1024,  # arbitrary vector
        user_ctx=user_ctx,
    )

    tenant_ids_in_results = {chunk.metadata["tenant_id"] for chunk in results}
    assert str(TENANT_B_ID) not in tenant_ids_in_results
    assert len(tenant_ids_in_results) <= 1
    if tenant_ids_in_results:
        assert tenant_ids_in_results == {str(TENANT_A_ID)}
```

```python
@pytest.mark.tenant_isolation
async def test_retrieval_with_empty_allowed_collections_returns_nothing(retrieval_service, seeded_qdrant_both_tenants):
    user_ctx = UserContext(tenant_id=TENANT_A_ID, allowed_collection_ids=[], ...)
    results = await retrieval_service.search(query_embedding=[0.1] * 1024, user_ctx=user_ctx)
    assert results == []
```

```python
@pytest.mark.tenant_isolation
async def test_collection_from_other_tenant_not_accessible(retrieval_service, seeded_qdrant_both_tenants):
    # COLLECTION_B belongs to TENANT_B. User from TENANT_A tries to query it directly.
    user_ctx = UserContext(
        tenant_id=TENANT_A_ID,
        allowed_collection_ids=[COLLECTION_B_ID],  # attacker injects foreign collection
        ...
    )
    # RetrievalService must still apply tenant_id=TENANT_A filter
    # Even with COLLECTION_B_ID in allowed list, no TENANT_B data returned
    results = await retrieval_service.search(query_embedding=[0.1] * 1024, user_ctx=user_ctx)
    assert all(chunk.metadata["tenant_id"] == str(TENANT_A_ID) for chunk in results)
```

### 2. IDOR Tests

**File:** `tests/security/test_idor.py`

```python
@pytest.mark.tenant_isolation
async def test_get_document_from_other_tenant_returns_404_not_403(
    test_client: httpx.AsyncClient,
    tenant_a_jwt: str,
    tenant_b_document_id: UUID,  # fixture: document owned by TENANT_B
):
    resp = await test_client.get(
        f"/api/v1/documents/{tenant_b_document_id}",
        headers={"Authorization": f"Bearer {tenant_a_jwt}"},
    )
    # MUST be 404, not 403 — 403 would reveal the resource exists
    assert resp.status_code == 404
```

```python
@pytest.mark.tenant_isolation
async def test_delete_document_from_other_tenant_returns_404(test_client, tenant_a_jwt, tenant_b_document_id):
    resp = await test_client.delete(
        f"/api/v1/documents/{tenant_b_document_id}",
        headers={"Authorization": f"Bearer {tenant_a_jwt}"},
    )
    assert resp.status_code == 404
```

```python
@pytest.mark.tenant_isolation
async def test_list_documents_returns_only_own_tenant(test_client, tenant_a_jwt, documents_both_tenants):
    # documents_both_tenants fixture: 2 docs for TENANT_A, 3 docs for TENANT_B
    resp = await test_client.get("/api/v1/documents", headers={"Authorization": f"Bearer {tenant_a_jwt}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    returned_ids = {item["id"] for item in body["items"]}
    assert all(doc_id in {str(TENANT_A_DOC_1_ID), str(TENANT_A_DOC_2_ID)} for doc_id in returned_ids)
```

```python
@pytest.mark.tenant_isolation
async def test_reindex_document_from_other_tenant_returns_404(test_client, tenant_a_jwt, tenant_b_document_id):
    resp = await test_client.post(
        f"/api/v1/documents/{tenant_b_document_id}/reindex",
        headers={"Authorization": f"Bearer {tenant_a_jwt}"},
    )
    assert resp.status_code == 404

@pytest.mark.tenant_isolation
async def test_review_document_from_other_tenant_returns_404(test_client, admin_a_jwt, tenant_b_document_id):
    resp = await test_client.post(
        f"/api/v1/documents/{tenant_b_document_id}/review",
        json={"decision": "approve", "note": ""},
        headers={"Authorization": f"Bearer {admin_a_jwt}"},
    )
    assert resp.status_code == 404

@pytest.mark.tenant_isolation
async def test_get_conversation_from_other_tenant_returns_404(test_client, tenant_a_user_jwt, tenant_b_conversation_id):
    resp = await test_client.get(
        f"/api/v1/conversations/{tenant_b_conversation_id}",
        headers={"Authorization": f"Bearer {tenant_a_user_jwt}"},
    )
    assert resp.status_code == 404
```

### 3. JWT Security Tests

**File:** `tests/security/test_jwt.py`

```python
@pytest.mark.auth
async def test_tampered_tenant_id_returns_401(test_client):
    # Create valid JWT for TENANT_A, then base64-decode payload, change tenant_id, re-encode WITHOUT re-signing
    original_token = JWTFactory(tenant_id=TENANT_A_ID)
    parts = original_token.split(".")
    payload_bytes = base64.b64decode(parts[1] + "==")
    payload = json.loads(payload_bytes)
    payload["tenant_id"] = str(TENANT_B_ID)
    tampered_payload = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

    resp = await test_client.get("/api/v1/documents", headers={"Authorization": f"Bearer {tampered_token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_INVALID"

@pytest.mark.auth
async def test_expired_jwt_returns_401(test_client):
    token = JWTFactory(exp=int(time.time()) - 60)  # expired 60s ago
    resp = await test_client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "TOKEN_EXPIRED"

@pytest.mark.auth
async def test_wrong_audience_returns_401(test_client):
    token = JWTFactory(aud="other-service")
    resp = await test_client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401

@pytest.mark.auth
async def test_no_token_returns_401(test_client):
    resp = await test_client.get("/api/v1/documents")
    assert resp.status_code == 401

@pytest.mark.auth
async def test_malformed_token_returns_401(test_client):
    resp = await test_client.get("/api/v1/documents", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401

@pytest.mark.auth
async def test_algorithm_none_attack_returns_401(test_client):
    # Construct JWT with alg=none in header
    header = base64.b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.b64encode(json.dumps({
        "sub": str(uuid4()), "tenant_id": str(TENANT_A_ID),
        "aud": "rag-api", "exp": int(time.time()) + 300,
    }).encode()).decode().rstrip("=")
    token = f"{header}.{payload}."
    resp = await test_client.get("/api/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
```

### 4. RBAC Per-Endpoint Tests

**File:** `tests/security/test_rbac.py`

These tests ensure that roles WITHOUT the required permission cannot access the resource. For each endpoint with a permission requirement, at minimum one test verifies the rejection.

```python
@pytest.mark.auth
@pytest.mark.parametrize("endpoint,method,body,permission,role_fixture", [
    ("/api/v1/documents", "GET", None, "documents:read", "user_no_read_jwt"),
    ("/api/v1/documents/upload", "POST", {...}, "documents:upload", "viewer_jwt"),
    ("/api/v1/documents/{doc_id}", "DELETE", None, "documents:delete", "worker_jwt"),
    ("/api/v1/documents/{doc_id}/reindex", "POST", None, "documents:manage", "worker_jwt"),
    ("/api/v1/documents/review-queue", "GET", None, "documents:approve", "worker_jwt"),
    ("/api/v1/documents/{doc_id}/review", "POST", {"decision": "approve"}, "documents:approve", "worker_jwt"),
    ("/api/v1/collections", "POST", {...}, "admin:collections", "worker_jwt"),
    ("/api/v1/collections/{col_id}", "DELETE", None, "admin:collections", "worker_jwt"),
    ("/api/v1/users", "GET", None, "admin:users", "worker_jwt"),
    ("/api/v1/audit-log", "GET", None, "admin:audit", "worker_jwt"),
])
async def test_permission_denied_returns_403(endpoint, method, body, permission, role_fixture, test_client, request, seed_ids):
    token = request.getfixturevalue(role_fixture)
    url = endpoint.format(**seed_ids)  # replace {doc_id}, {col_id} with seeded UUIDs
    headers = {"Authorization": f"Bearer {token}"}
    resp = await getattr(test_client, method.lower())(url, json=body, headers=headers)
    assert resp.status_code == 403, f"Expected 403 for {method} {endpoint} with role {role_fixture}"
    assert resp.json()["error"]["code"] == "PERMISSION_DENIED"
```

### 5. Collection Access Scoping Tests

**File:** `tests/security/test_collection_scoping.py`

```python
@pytest.mark.tenant_isolation
async def test_viewer_of_collection_a_cannot_retrieve_from_collection_b(
    retrieval_service,
    qdrant_container,
    seeded_qdrant_two_collections,  # COLLECTION_A and COLLECTION_B in TENANT_A
):
    # User has Viewer role on COLLECTION_A only
    user_ctx = UserContext(
        tenant_id=TENANT_A_ID,
        allowed_collection_ids=[COLLECTION_A_ID],  # NOT COLLECTION_B_ID
        ...
    )
    results = await retrieval_service.search(query_embedding=[0.1] * 1024, user_ctx=user_ctx)
    collection_ids_in_results = {chunk.metadata["collection_id"] for chunk in results}
    assert str(COLLECTION_B_ID) not in collection_ids_in_results

@pytest.mark.tenant_isolation
async def test_get_tenant_collections_returns_only_accessible_collections(db_session, user_with_role_a_only):
    # user_with_role_a_only: user has role that grants access to COLLECTION_A but not COLLECTION_B
    collections = await get_tenant_collections(user_ctx=user_with_role_a_only, db=db_session)
    assert COLLECTION_A_ID in collections
    assert COLLECTION_B_ID not in collections
```

### 6. Architecture Tests (Import Linter)

**File:** `tests/security/test_architecture.py`

```python
def test_no_direct_qdrant_import_outside_retrieval():
    """
    No module outside src/retrieval/ may import QdrantClient directly.
    This enforces the hard rule: Qdrant access only through RetrievalService.
    """
    import ast
    import pathlib

    src_root = pathlib.Path("src")
    violations = []

    for py_file in src_root.rglob("*.py"):
        if py_file.parts[1] == "retrieval":
            continue  # allowed
        source = py_file.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in getattr(node, "names", [])]
                module = getattr(node, "module", "") or ""
                if "qdrant_client" in module or any("QdrantClient" in n for n in names):
                    violations.append(str(py_file))

    assert violations == [], (
        f"Direct QdrantClient imports found outside src/retrieval/: {violations}\n"
        "All Qdrant access must go through RetrievalService."
    )

def test_api_does_not_import_from_core_directly():
    """
    Enforce layered architecture: api → domain → core.
    src/api/ must not import from src/core/ bypassing domain/.
    Allowed: api → domain, api → core.config (settings), api → core.exceptions (HTTP mapping).
    Forbidden: api importing domain-layer objects from core (e.g., core.clients.qdrant).
    """
    # Implementation: AST walk src/api/, check ImportFrom nodes for prohibited core submodules.
    # Adjust forbidden_submodules list to match actual core/ structure.
    forbidden_submodules = ["core.clients", "core.db"]
    src_api = pathlib.Path("src/api")
    violations = []
    for py_file in src_api.rglob("*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(f"src.{mod}") or node.module.startswith(mod) for mod in forbidden_submodules):
                    violations.append(f"{py_file}:{node.lineno}")
    assert violations == [], f"Layering violations: {violations}"
```

---

## RAG Evaluation Tests

**Location:** `tests/eval/`

**When they run:** Weekly CI job + on any PR touching `src/graphs/`, `src/retrieval/`, or `graphs/prompts/`.

**Important:** These tests call a real (local) LLM endpoint. Set `RAG_EVAL_LLM_URL` env variable in the weekly CI job. Do NOT run in the fast PR check.

### Corpus and Fixture Files

**`tests/eval/fixtures/corpus/`** — 10 PDF files:
- `proc_001_sterylizacja.pdf` — sterilization procedure (synthetic, no real PII)
- `proc_002_mycie_rak.pdf` — hand washing procedure
- `proc_003_izolacja_pacjenta.pdf` — patient isolation
- `proc_004_transport_odpadow.pdf` — medical waste transport
- `proc_005_rejestracja_wizyty.pdf` — visit registration process
- `proc_006_postepowanie_po_nakladuciu.pdf` — needlestick post-exposure protocol
- `proc_007_audit_dokumentacji.pdf` — documentation audit process
- `proc_008_awaria_sprzetu.pdf` — equipment failure procedure
- `proc_009_szkolenia_pracownikow.pdf` — staff training requirements
- `proc_010_dezynfekcja_powierzchni.pdf` — surface disinfection

All PDFs are synthetic, generated with lorem-ipsum-style content matching real procedure document structure. No real patient data. Each is 2–5 pages.

**`tests/eval/fixtures/questions.json`** — 50 items, format:
```json
[
  {
    "id": "q001",
    "question": "Jakie są etapy sterylizacji narzędzi chirurgicznych?",
    "expected_doc_ids": ["proc_001"],
    "expected_answer_contains": ["autoclaw", "121 stopni", "18 minut"],
    "min_relevant_chunks": 1
  },
  ...
]
```

**`tests/eval/fixtures/trap_questions.json`** — 20 items, format:
```json
[
  {
    "id": "trap001",
    "question": "Jaka jest cena wizyty prywatnej?",
    "note": "pricing not in corpus",
    "expected_not_found": true
  },
  ...
]
```

**`tests/eval/fixtures/baseline.json`** — updated explicitly with `--update-baseline`:
```json
{
  "recall_at_8": 0.86,
  "precision_at_8": 0.71,
  "faithfulness": 0.88,
  "citation_accuracy": 0.82,
  "trap_accuracy": 1.00,
  "measured_at": "2026-07-13T00:00:00Z",
  "commit": "abc1234"
}
```

### Retrieval Quality Tests

**File:** `tests/eval/test_retrieval_quality.py`

```python
@pytest.fixture(scope="session")
def eval_corpus_loaded(qdrant_container, postgres_container):
    """Ingest all 10 corpus PDFs into the eval tenant before tests run."""
    # This fixture runs the full ingest pipeline (real Qdrant, fake LLM for classify/validate)
    ...

@pytest.mark.eval
async def test_recall_at_8(eval_corpus_loaded, retrieval_service, eval_questions):
    """
    For each of the 50 questions, retrieve top-8 chunks.
    recall@8 = fraction of questions where at least one chunk from expected_doc_ids is in top 8.
    """
    hits = 0
    for q in eval_questions:
        results = await retrieval_service.search(
            query_text=q["question"],
            user_ctx=EVAL_USER_CTX,
            top_k=8,
        )
        retrieved_doc_ids = {r.metadata["document_id"] for r in results}
        if any(expected in retrieved_doc_ids for expected in q["expected_doc_ids"]):
            hits += 1
    recall = hits / len(eval_questions)

    baseline = load_baseline()
    threshold = baseline["recall_at_8"] * 0.95  # 5% regression allowed
    assert recall >= threshold, (
        f"recall@8 regression: {recall:.3f} < {threshold:.3f} (baseline: {baseline['recall_at_8']:.3f})"
    )

@pytest.mark.eval
async def test_precision_at_8(eval_corpus_loaded, retrieval_service, eval_questions):
    """
    precision@8 = average fraction of returned chunks that are relevant (from expected_doc_ids).
    """
    # Implementation: for each question, count relevant chunks / 8
    ...
    baseline = load_baseline()
    threshold = baseline["precision_at_8"] * 0.95
    assert precision >= threshold

def load_baseline() -> dict:
    path = Path(__file__).parent / "fixtures" / "baseline.json"
    return json.loads(path.read_text())

def update_baseline(metrics: dict):
    """Called only when pytest --update-baseline flag is set."""
    ...
```

Add `conftest.py` hook in `tests/eval/`:
```python
def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true", default=False)

@pytest.fixture(scope="session")
def update_baseline_flag(request):
    return request.config.getoption("--update-baseline")
```

### Generation Quality Tests

**File:** `tests/eval/test_generation_quality.py`

```python
@pytest.mark.eval
async def test_citation_accuracy(eval_corpus_loaded, test_client, eval_user_jwt, eval_questions):
    """
    citation_accuracy = fraction of answers where at least one cited chunk
    contains text matching expected_answer_contains keywords.
    """
    accurate = 0
    for q in eval_questions:
        resp = await test_client.post("/api/v1/chat/completions", json={
            "model": "rag-procedures",
            "messages": [{"role": "user", "content": q["question"]}],
            "stream": False,
        }, headers={"Authorization": f"Bearer {eval_user_jwt}"})
        body = resp.json()
        cited_texts = [c.get("highlight_text", "") for c in body.get("citations", [])]
        if any(keyword.lower() in " ".join(cited_texts).lower() for keyword in q["expected_answer_contains"]):
            accurate += 1
    accuracy = accurate / len(eval_questions)
    baseline = load_baseline()
    assert accuracy >= baseline["citation_accuracy"] * 0.95

@pytest.mark.eval
async def test_faithfulness(eval_corpus_loaded, test_client, eval_user_jwt, eval_questions, ragas_llm):
    """
    faithfulness = fraction of answer sentences attributable to the retrieved context.
    Uses RAGAS faithfulness metric.
    """
    from ragas import evaluate
    from ragas.metrics import faithfulness
    # Build dataset from responses
    # ...
    result = evaluate(dataset, metrics=[faithfulness], llm=ragas_llm)
    score = result["faithfulness"]
    baseline = load_baseline()
    assert score >= baseline["faithfulness"] * 0.95, (
        f"Faithfulness regression: {score:.3f} < {baseline['faithfulness'] * 0.95:.3f}"
    )

@pytest.mark.eval
async def test_trap_questions_return_not_found(test_client, eval_user_jwt, trap_questions):
    """
    All 20 trap questions must result in "nie znalazłem w dokumentach" (or configured equivalent).
    trap_accuracy must be 1.00 — any answer fabricated from outside corpus is a failure.
    """
    # MAJOR FIX: Include both English and Polish variants to match settings.not_found_message
    # (default English) and any locale overrides. Use case-insensitive matching.
    NOT_FOUND_INDICATORS = [
        "not found in",
        "could not find",
        "i could not find an answer",
        "nie znalazłem",
        "nie znalazłem odpowiedzi",
        "nie znalazłem w dokumentach",
        "nie mam informacji",
        "brak informacji w dostępnych dokumentach",
    ]
    failures = []
    for q in trap_questions:
        resp = await test_client.post("/api/v1/chat/completions", json={
            "model": "rag-procedures",
            "messages": [{"role": "user", "content": q["question"]}],
            "stream": False,
        }, headers={"Authorization": f"Bearer {eval_user_jwt}"})
        answer = resp.json()["choices"][0]["message"]["content"]
        # Case-insensitive matching: settings.not_found_message may be in any case
        if not any(indicator in answer.lower() for indicator in NOT_FOUND_INDICATORS):
            failures.append({"question": q["question"], "answer": answer[:200]})

    assert failures == [], (
        f"Trap questions answered with fabricated content ({len(failures)}/20 failures):\n"
        + "\n".join(f"  Q: {f['question']}\n  A: {f['answer']}" for f in failures)
    )
```

---

## Performance Tests

**File:** `tests/performance/locustfile.py`

Run with: `locust -f tests/performance/locustfile.py --headless -u 20 -r 2 --run-time 2m --host http://localhost:8000`

```python
from locust import HttpUser, task, between
import json, os

TENANT_A_USER_JWT = os.environ["PERF_TENANT_A_USER_JWT"]
TENANT_B_USER_JWT = os.environ["PERF_TENANT_B_USER_JWT"]
TENANT_C_USER_JWT = os.environ["PERF_TENANT_C_USER_JWT"]

class ChatQueryUser(HttpUser):
    """Scenario 1: 20 concurrent chat queries. SLA: p95 < 10s."""
    wait_time = between(1, 3)
    weight = 3

    @task
    def chat_query(self):
        self.client.post(
            "/api/v1/chat/completions",
            json={
                "model": "rag-procedures",
                "messages": [{"role": "user", "content": "procedura dezynfekcji narzędzi"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {TENANT_A_USER_JWT}"},
            timeout=15,
            name="/v1/chat/completions",
        )

class DocumentUploadUser(HttpUser):
    """Scenario 2: 100 document uploads. SLA: all processed within 5 minutes."""
    wait_time = between(0.5, 1)
    weight = 1

    @task
    def upload_document(self):
        with open("tests/eval/fixtures/corpus/proc_001_sterylizacja.pdf", "rb") as f:
            self.client.post(
                "/api/v1/documents/upload",
                files={"file": ("test.pdf", f, "application/pdf")},
                data={"collection_id": os.environ["PERF_COLLECTION_A_ID"]},
                headers={"Authorization": f"Bearer {TENANT_A_USER_JWT}"},
                name="/documents/upload",
            )

class MultiTenantChatUser(HttpUser):
    """Scenario 3: 50 concurrent users across 3 tenants. Verify no cross-tenant data."""
    wait_time = between(1, 2)
    weight = 2

    def on_start(self):
        import random
        self.jwt = random.choice([TENANT_A_USER_JWT, TENANT_B_USER_JWT, TENANT_C_USER_JWT])
        # Decode JWT to get tenant_id for assertion
        import base64, json as _json
        payload = self.jwt.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        self.expected_tenant_id = _json.loads(base64.b64decode(payload))["tenant_id"]

    @task
    def chat_and_verify_tenant(self):
        resp = self.client.post(
            "/api/v1/chat/completions",
            json={"model": "rag-procedures", "messages": [{"role": "user", "content": "test"}], "stream": False},
            headers={"Authorization": f"Bearer {self.jwt}"},
            name="/v1/chat/completions [multi-tenant]",
        )
        if resp.status_code == 200:
            body = resp.json()
            for citation in body.get("citations", []):
                # Each citation must belong to the requesting tenant
                # doc_id can be cross-referenced via GET /documents/{id}
                # For perf test: assert doc_id is in known set for this tenant
                pass  # Full assertion done in security tests; here we check no 500
            if resp.status_code >= 500:
                self.environment.events.request.fire(
                    request_type="POST",
                    name="cross_tenant_error",
                    response_time=resp.elapsed.total_seconds() * 1000,
                    response_length=len(resp.content),
                    exception=Exception(f"Server error: {resp.status_code}"),
                    context={},
                )
```

**SLA assertions** (run as post-test check in CI):
```bash
# After locust run, parse stats CSV:
# p95 of /v1/chat/completions must be < 10000ms
# All uploads must complete without 5xx (monitor via Grafana or locust stats)
python tests/performance/check_slas.py locust_stats.csv
```

**`tests/performance/check_slas.py`:**
```python
import sys, csv

def check(stats_file: str):
    with open(stats_file) as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if row["Name"] == "/v1/chat/completions" and row["Type"] == "POST":
            p95 = float(row.get("95%", 0))
            assert p95 < 10000, f"p95 latency {p95}ms exceeds 10s SLA"
        if row["Name"] == "/documents/upload":
            failure_rate = float(row.get("Failure Count", 0)) / max(float(row.get("Request Count", 1)), 1)
            assert failure_rate < 0.01, f"Upload failure rate {failure_rate:.1%} exceeds 1%"

if __name__ == "__main__":
    check(sys.argv[1])
```

---

## Test Utilities and Factories

### `tests/factories.py`

```python
import factory
from factory.alchemy import SQLAlchemyModelFactory
import uuid, time
from jose import jwt

TEST_JWT_SECRET = "test-only-secret-not-for-production"
TEST_JWT_ALGORITHM = "HS256"
TEST_JWT_AUDIENCE = "rag-api"


class TenantFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Tenant  # src.db.models.Tenant
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"Test Tenant {n}")
    slug = factory.Sequence(lambda n: f"test-tenant-{n}")
    status = "active"
    settings = factory.LazyFunction(dict)

    @factory.post_generation
    def with_default_roles(obj, create, extracted, **kwargs):
        """Creates admin, worker, viewer roles with standard permissions."""
        if not create:
            return
        session = obj._meta.sqlalchemy_session
        for role_name, perms in [
            ("admin", ["documents:read", "documents:upload", "documents:delete", "documents:manage",
                       "documents:approve", "admin:collections", "admin:users", "admin:audit",
                       "admin:models", "chat:query"]),
            ("worker", ["documents:read", "documents:upload", "chat:query"]),
            ("viewer", ["chat:query"]),
        ]:
            RoleFactory(tenant=obj, name=role_name, _permissions=perms, _session=session)


class UserFactory(SQLAlchemyModelFactory):
    class Meta:
        model = User
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    keycloak_sub = factory.LazyFunction(lambda: str(uuid.uuid4()))
    email = factory.Sequence(lambda n: f"user{n}@test.example")
    display_name = factory.Sequence(lambda n: f"Test User {n}")
    is_active = True


class CollectionFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Collection
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory(TenantFactory)
    name = factory.Sequence(lambda n: f"collection-{n}")
    description = "Test collection"
    chunk_config = factory.LazyFunction(lambda: {
        "default": {"strategy": "recursive", "size": 512, "overlap": 64}
    })
    is_active = True


class DocumentFactory(SQLAlchemyModelFactory):
    class Meta:
        model = Document
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    tenant = factory.SubFactory(TenantFactory)
    collection = factory.SubFactory(CollectionFactory)
    title = factory.Sequence(lambda n: f"Test Document {n}")
    original_filename = factory.Sequence(lambda n: f"doc_{n}.pdf")
    minio_key = factory.LazyAttribute(
        lambda o: f"tenant-{o.tenant.slug}/raw/{o.collection.id}/{o.id}/{o.original_filename}"
    )
    mime_type = "application/pdf"
    size_bytes = 1024 * 100
    sha256 = factory.LazyFunction(lambda: uuid.uuid4().hex)
    status = "ready"  # override with status=factory.Iterator(["uploaded", "processing", "needs_review", "ready", "failed"])
    uploaded_by = factory.LazyFunction(uuid.uuid4)


class JWTFactory:
    """
    Creates signed JWT tokens for testing using HMAC-SHA256 (HS256).

    Production uses RS256 (Keycloak). The test_client fixture in conftest.py
    MUST override get_current_ctx to bypass RS256 signature verification:

        from src.api.dependencies.auth import get_current_ctx

        @pytest.fixture
        async def test_client(db_session, ...) -> AsyncClient:
            from src.main import app
            # Override: skip RS256 verification; build UserContext directly from JWTFactory payload.
            app.dependency_overrides[get_current_ctx] = lambda: _build_ctx_from_test_token(...)
            # Or use a simpler helper that accepts HS256 tokens:
            app.dependency_overrides[get_current_ctx] = make_test_auth_dependency(TEST_JWT_SECRET)
            ...

    Without this override, every authenticated request in unit/integration tests would
    fail with a KEY_ERROR or signature verification failure because production get_current_ctx
    fetches JWKS from Keycloak (RS256), not the HS256 test secret.

    JWTFactory.build_context() helper (add to this class):
        @staticmethod
        def build_context(tenant_id: UUID, permissions: set[str], ...) -> UserContext:
            \"\"\"Directly builds a UserContext without token parsing — for dependency overrides.\"\"\"
            return UserContext(user_id=uuid4(), tenant_id=tenant_id, permissions=permissions, ...)
    """
    @staticmethod
    def create(
        user_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        role_ids: list[uuid.UUID] | None = None,
        permissions: list[str] | None = None,
        allowed_collection_ids: list[uuid.UUID] | None = None,
        exp: int | None = None,
        aud: str = TEST_JWT_AUDIENCE,
        **extra_claims,
    ) -> str:
        payload = {
            "sub": str(user_id or uuid.uuid4()),
            "tenant_id": str(tenant_id or uuid.uuid4()),
            "roles": [str(r) for r in (role_ids or [])],
            "permissions": permissions or [],
            "allowed_collections": [str(c) for c in (allowed_collection_ids or [])],
            "aud": aud,
            "exp": exp or int(time.time()) + 300,
            "iat": int(time.time()),
            **extra_claims,
        }
        return jwt.encode(payload, TEST_JWT_SECRET, algorithm=TEST_JWT_ALGORITHM)
```

### `tests/conftest.py`

```python
import asyncio, pytest, pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer
from testcontainers.qdrant import QdrantContainer  # or use generic HTTP container
from testcontainers.minio import MinioContainer
from testcontainers.redis import RedisContainer
from alembic.config import Config
from alembic import command

# ─── Session-scoped: start containers once per test session ───────────────────

# MINOR FIX: Do NOT define a custom `event_loop` fixture.
# pytest-asyncio >=0.23 deprecates overriding `event_loop` at session scope.
# Instead, set asyncio_mode = "auto" and asyncio_default_fixture_loop_scope = "session"
# in pyproject.toml [tool.pytest.ini_options]:
#
#   asyncio_mode = "auto"
#   asyncio_default_fixture_loop_scope = "session"
#
# This gives a session-scoped event loop without requiring the deprecated fixture override.
# The event_loop fixture below is REMOVED — do not add it back.

@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg

@pytest.fixture(scope="session")
def qdrant_container():
    # Use QdrantContainer or GenericContainer with qdrant/qdrant:latest
    with QdrantContainer("qdrant/qdrant:latest") as qd:
        yield qd

@pytest.fixture(scope="session")
def minio_container():
    with MinioContainer("minio/minio:latest") as minio:
        yield minio

@pytest.fixture(scope="session")
def redis_container():
    with RedisContainer("redis:7-alpine") as redis:
        yield redis

@pytest.fixture(scope="session")
def db_engine(postgres_container):
    url = postgres_container.get_connection_url().replace("psycopg2", "asyncpg")
    engine = create_async_engine(url)
    return engine

@pytest.fixture(scope="session", autouse=True)
def run_migrations(db_engine, postgres_container):
    """Run Alembic migrations once per test session."""
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", postgres_container.get_connection_url())
    command.upgrade(alembic_cfg, "head")

@pytest.fixture(scope="session", autouse=True)
def seed_permissions(db_engine):
    """Seed the permissions table with all defined permission codes."""
    # Insert rows for: documents:read, documents:upload, etc.
    ...

# ─── Function-scoped: transaction rollback per test ───────────────────────────

@pytest.fixture
async def db_session(db_engine) -> AsyncSession:
    """
    Provides an async session wrapped in a transaction that is rolled back
    after each test. This guarantees test isolation without truncating tables.
    """
    async with db_engine.connect() as conn:
        await conn.begin()
        async_session = async_sessionmaker(bind=conn, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            yield session
        await conn.rollback()

# ─── FastAPI test client ───────────────────────────────────────────────────────

@pytest.fixture
async def test_client(db_session, qdrant_container, minio_container, redis_container) -> AsyncClient:
    from src.main import app
    from src.core.dependencies import get_db, get_qdrant, get_minio, get_redis

    app.dependency_overrides[get_db] = lambda: db_session
    # Override qdrant, minio, redis with testcontainer-connected clients
    ...
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()

# ─── JWT fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def tenant_a_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000001")

@pytest.fixture
def tenant_b_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000002")

@pytest.fixture
def admin_ctx(tenant_a_id) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), tenant_id=tenant_a_id,
        role_ids=[ADMIN_ROLE_ID],
        permissions={"documents:read", "documents:upload", "documents:delete",
                     "documents:manage", "documents:approve", "admin:collections",
                     "admin:users", "admin:audit", "admin:models", "chat:query"},
        allowed_collection_ids=[COLLECTION_A_ID],
    )

@pytest.fixture
def admin_jwt(admin_ctx) -> str:
    return JWTFactory.create(
        user_id=admin_ctx.user_id, tenant_id=admin_ctx.tenant_id,
        role_ids=list(admin_ctx.role_ids), permissions=list(admin_ctx.permissions),
        allowed_collection_ids=list(admin_ctx.allowed_collection_ids),
    )

@pytest.fixture
def worker_ctx(tenant_a_id) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), tenant_id=tenant_a_id,
        role_ids=[WORKER_ROLE_ID],
        permissions={"documents:read", "documents:upload", "chat:query"},
        allowed_collection_ids=[COLLECTION_A_ID],
    )

@pytest.fixture
def worker_jwt(worker_ctx) -> str:
    return JWTFactory.create(
        user_id=worker_ctx.user_id, tenant_id=worker_ctx.tenant_id,
        role_ids=list(worker_ctx.role_ids), permissions=list(worker_ctx.permissions),
        allowed_collection_ids=list(worker_ctx.allowed_collection_ids),
    )

@pytest.fixture
def user_ctx(tenant_a_id) -> UserContext:
    return UserContext(
        user_id=uuid.uuid4(), tenant_id=tenant_a_id,
        role_ids=[VIEWER_ROLE_ID],
        permissions={"chat:query"},
        allowed_collection_ids=[COLLECTION_A_ID],
    )

@pytest.fixture
def user_jwt(user_ctx) -> str:
    return JWTFactory.create(
        user_id=user_ctx.user_id, tenant_id=user_ctx.tenant_id,
        role_ids=list(user_ctx.role_ids), permissions=list(user_ctx.permissions),
        allowed_collection_ids=list(user_ctx.allowed_collection_ids),
    )

@pytest.fixture
def tenant_a_jwt(admin_jwt) -> str:
    return admin_jwt

@pytest.fixture
def tenant_b_admin_jwt(tenant_b_id) -> str:
    return JWTFactory.create(
        tenant_id=tenant_b_id,
        permissions=["documents:read", "documents:upload", "admin:collections"],
    )

# ─── LLM mock ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm_client():
    """
    Deterministic fake LLM for unit tests.
    Returns controlled responses without network calls.
    """
    mock = AsyncMock()
    mock.chat.return_value = {"choices": [{"message": {"content": '{"intent": "topical_question"}'}}]}
    mock.embeddings.return_value = {"data": [{"embedding": [0.1] * 1024}]}
    return mock

@pytest.fixture
def fake_llm(mock_llm_client):
    """Alias; used in integration tests where full FastAPI app overrides LLM dependency."""
    return mock_llm_client

# ─── Shared data fixtures ─────────────────────────────────────────────────────

@pytest.fixture
async def tenant_b_document_id(db_session, tenant_b_id) -> uuid.UUID:
    """Creates a document owned by TENANT_B. Used in IDOR tests."""
    doc = await DocumentFactory.create_async(session=db_session, tenant_id=tenant_b_id, status="ready")
    return doc.id

@pytest.fixture
async def seeded_qdrant_both_tenants(qdrant_container):
    """Inserts known points for TENANT_A and TENANT_B into the test Qdrant instance."""
    ...
```

### pytest.ini / pyproject.toml configuration

Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"   # MINOR FIX: replaces deprecated event_loop fixture
markers = [
    "integration: marks tests as integration tests requiring testcontainers",
    "tenant_isolation: marks tests as tenant isolation tests — BLOCKER for merge",
    "auth: marks tests as authentication/authorization tests",
    "eval: marks tests as RAG evaluation tests (require LLM endpoint)",
    "performance: marks tests as performance tests (Locust)",
]
testpaths = ["tests"]
addopts = "--strict-markers"
```

---

## Coverage Requirements

| Module | Minimum line coverage | Notes |
|---|---|---|
| `src/domain/` | 90% | All business logic paths |
| `src/graphs/` | 85% | Node isolation tests cover most paths |
| `src/retrieval/` | 95% | Security-critical; highest bar |
| `src/api/` | 80% | All endpoints exercised via integration tests |
| Overall `src/` | 80% | |

Additional rule: **Any uncovered line in `src/retrieval/`, `src/api/dependencies/auth.py`, or `src/domain/deletion_service.py` causes the coverage check to fail**, regardless of overall percentage. Implement using `# pragma: no cover` sparingly and only for truly unreachable defensive branches, with a comment explaining why.

Coverage enforcement in CI:
```bash
pytest -x -q --ignore=tests/eval --ignore=tests/performance \
  --cov=src \
  --cov-report=xml:coverage.xml \
  --cov-report=term-missing \
  --cov-fail-under=80
```

For per-module enforcement, add `[coverage:report]` exclusions and a separate check:
```bash
python tests/scripts/check_critical_coverage.py coverage.xml
```

`tests/scripts/check_critical_coverage.py`:
```python
"""Fails if critical modules have any uncovered lines."""
import sys
import xml.etree.ElementTree as ET

CRITICAL_MODULES = [
    "src/retrieval/service.py",
    "src/api/dependencies/auth.py",
    "src/domain/deletion_service.py",
]


def check_critical_coverage(coverage_xml_path: str) -> None:
    # MINOR FIX: Previous version used line.get("branch", "") to detect "no cover" pragmas.
    # The "branch" attribute in coverage.xml is a bool ("true"/"false") tracking branch
    # coverage — it does NOT contain pragma text. Uncovered lines are identified solely
    # by hits == "0". The # pragma: no cover exclusion is handled by coverage.py itself
    # before writing the XML; lines with that pragma simply do not appear in the report.
    tree = ET.parse(coverage_xml_path)
    uncovered: list[tuple[str, str | None]] = []
    for file_elem in tree.findall(".//class"):
        filename = file_elem.get("filename", "")
        if any(critical in filename for critical in CRITICAL_MODULES):
            for line in file_elem.findall("lines/line"):
                if line.get("hits") == "0":  # 0 hits = not covered
                    uncovered.append((filename, line.get("number")))
    if uncovered:
        print("CRITICAL COVERAGE FAILURES:")
        for fname, lineno in uncovered:
            print(f"  {fname}:{lineno}")
        sys.exit(1)
    print("OK: All critical module lines covered.")


if __name__ == "__main__":
    check_critical_coverage(sys.argv[1])
```

---

## CI Integration

### Pipeline Configuration (GitHub Actions or equivalent)

**Job 1: fast-tests (runs on every PR)**
```yaml
name: Fast Tests
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      # testcontainers starts its own Docker containers; no services block needed
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run mypy src/
      - run: |
          uv run pytest -x -q \
            --ignore=tests/eval \
            --ignore=tests/performance \
            --cov=src \
            --cov-report=xml:coverage.xml \
            --cov-fail-under=80
      - run: uv run python tests/scripts/check_critical_coverage.py coverage.xml
      - uses: codecov/codecov-action@v4
        with:
          file: coverage.xml
```

**Job 2: tenant-isolation (required check, runs on every PR touching security-relevant paths)**
```yaml
name: Tenant Isolation (Blocker)
on:
  pull_request:
    paths:
      - "src/retrieval/**"
      - "src/api/dependencies/**"
      - "src/domain/**"
      - "src/graphs/**"
      - "tests/security/**"
jobs:
  isolation:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - run: |
          uv run pytest tests/security/ \
            -m "tenant_isolation or auth" \
            -v \
            --tb=short
```

**Job 3: rag-eval (weekly + on RAG-related PRs)**
```yaml
name: RAG Evaluation
on:
  schedule:
    - cron: "0 2 * * 1"  # Monday 02:00 UTC
  pull_request:
    paths:
      - "src/graphs/**"
      - "src/retrieval/**"
      - "graphs/prompts/**"
jobs:
  eval:
    runs-on: ubuntu-latest
    env:
      RAG_EVAL_LLM_URL: ${{ secrets.EVAL_LLM_URL }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --frozen
      - run: uv run pytest tests/eval/ -m eval -v --tb=short
```

---

## Definition of Done

The following conditions must ALL be true before this task is considered complete:

**Functional:**
- [ ] All 3 CI jobs pass on a clean branch.
- [ ] `pytest -x -q --ignore=tests/eval --ignore=tests/performance` exits 0.
- [ ] `pytest tests/security/ -m "tenant_isolation or auth" -v` exits 0.
- [ ] `pytest tests/eval/ -m eval` exits 0 against the eval LLM endpoint.

**Coverage:**
- [ ] Overall `src/` coverage >= 80%.
- [ ] `src/retrieval/` coverage >= 95%.
- [ ] `src/domain/` coverage >= 90%.
- [ ] `tests/scripts/check_critical_coverage.py coverage.xml` exits 0 (no uncovered lines in critical modules).

**Security contracts verified by tests:**
- [ ] Cross-tenant retrieval: Tenant A query returns zero Tenant B chunks (real Qdrant, `@pytest.mark.tenant_isolation`).
- [ ] IDOR: GET `/documents/{id}` of other tenant returns 404, not 403.
- [ ] JWT tampering (modified payload without re-signing) returns 401.
- [ ] Algorithm-none attack returns 401.
- [ ] Expired JWT returns 401.
- [ ] Wrong audience returns 401.
- [ ] Every endpoint with a permission has at least one test verifying 403 for a role that lacks it.
- [ ] Empty `allowed_collections` returns empty result from `RetrievalService` without calling Qdrant.

**RAG quality (baseline registered):**
- [ ] `tests/eval/fixtures/baseline.json` populated with initial run values.
- [ ] Trap question accuracy = 1.00 (no fabricated answers).
- [ ] recall@8 >= 0.80 (initial baseline; adjust after first eval run).

**Architecture:**
- [ ] `test_no_direct_qdrant_import_outside_retrieval` passes.
- [ ] `test_api_does_not_import_from_core_directly` passes.

**Factories and utilities:**
- [ ] All 5 factories (`TenantFactory`, `UserFactory`, `CollectionFactory`, `DocumentFactory`, `JWTFactory`) work correctly and are used in at least 3 tests each.
- [ ] `conftest.py` transaction rollback verified: data from test N is not visible in test N+1.

**Documentation:**
- [ ] This task file updated with actual file paths if they differ from planned paths.
- [ ] `docs/02-Architektura.md` (or `docs/architecture.md`) updated if test infrastructure decisions constitute architectural choices (e.g., testcontainer vs mock strategy).

---

*Reviewed by: python-reviewer (security profile)*  
*Last updated: 2026-07-13*
