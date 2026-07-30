"""Unit tests for node_extract_entities.

Coverage:
- test_entity_extraction_succeeds_with_mocked_llm
- test_entity_extraction_failure_is_nonfatal
- test_entity_extraction_disabled_skips_node
- test_entity_extraction_no_pii_in_logs
- test_entity_extraction_empty_chunks_returns_none
- test_entity_extraction_relation_failure_is_nonfatal
- test_entity_extraction_invalid_entity_type_filtered
- test_entity_extraction_batch_partitioning
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_extract_entities import node_extract_entities
from src.graphs.ingest_graph.state import ChunkData, IngestState

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_point_id(idx: int) -> uuid.UUID:
    digest = hashlib.sha256(f"{DOCUMENT_ID}:{idx}".encode()).hexdigest()
    return uuid.UUID(digest[:32])


def _make_chunks(n: int = 3) -> list[ChunkData]:
    return [
        ChunkData(
            chunk_index=i,
            text=f"Metformin treats type 2 diabetes. Chunk {i}.",
            page=i + 1,
            section=None,
            token_count=15,
            point_id=_make_point_id(i),
        )
        for i in range(n)
    ]


def _make_state(**kwargs: Any) -> IngestState:
    defaults: dict[str, Any] = {
        "document_id": DOCUMENT_ID,
        "tenant_id": TENANT_ID,
        "collection_id": COLLECTION_ID,
        "minio_key": f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        "job_id": JOB_ID,
        "chunks": _make_chunks(),
    }
    defaults.update(kwargs)
    return IngestState(**defaults)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_collection(graph_rag_enabled: bool = True) -> MagicMock:
    collection = MagicMock()
    collection.chunk_config = {"graph_rag_enabled": graph_rag_enabled}
    collection.embedding_model_id = uuid.uuid4()
    return collection


def _make_model() -> MagicMock:
    model = MagicMock()
    model.model_id = "llama3:8b"
    model.endpoint_url = "http://ollama:11434/v1"
    return model


def _make_llm_response(content: str) -> MagicMock:
    """Build a mock LLM response with a specific JSON string content."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _make_config(session: Any, llm: Any) -> dict:
    return {"configurable": {"db": session, "llm": llm}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_extraction_succeeds_with_mocked_llm() -> None:
    """node_extract_entities returns extracted_entities when LLM succeeds."""
    session = _make_session()
    llm = AsyncMock()

    entity_payload = json.dumps(
        {
            "entities": [
                {
                    "type": "drug",
                    "name": "metformin",
                    "icd_code": None,
                    "atc_code": "A10BA02",
                    "confidence": 0.95,
                    "surface_forms": ["metformin", "Metformin"],
                }
            ]
        }
    )
    relation_payload = json.dumps(
        {
            "relations": [
                {
                    "source": "metformin",
                    "target": "type 2 diabetes",
                    "relation": "treats",
                    "confidence": 0.9,
                    "evidence_sentence": "Metformin treats type 2 diabetes.",
                }
            ]
        }
    )
    # LLM is called twice per batch: entity call then relation call
    llm.chat_completion = AsyncMock(
        side_effect=[
            _make_llm_response(entity_payload),
            _make_llm_response(relation_payload),
        ]
    )

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="You are a medical entity extractor.",
        ),
    ):
        state = _make_state()
        result = await node_extract_entities(state, _make_config(session, llm))

    assert result["extracted_entities"] is not None
    extracted = result["extracted_entities"]
    assert isinstance(extracted, list)
    assert len(extracted) == 1
    batch = extracted[0]
    assert "entities" in batch
    assert len(batch["entities"]) == 1
    entity = batch["entities"][0]
    assert entity["name"] == "metformin"
    assert entity["type"] == "drug"
    assert entity["atc_code"] == "A10BA02"
    assert "metformin" in entity["surface_forms"]


@pytest.mark.asyncio
async def test_entity_extraction_failure_is_nonfatal() -> None:
    """A complete LLM failure during extraction returns None and does not raise."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock(side_effect=RuntimeError("LLM unreachable"))

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state()
        # Must NOT raise — entity extraction is non-fatal enrichment
        result = await node_extract_entities(state, _make_config(session, llm))

    assert result == {"extracted_entities": None}


@pytest.mark.asyncio
async def test_entity_extraction_disabled_skips_node() -> None:
    """When graph_rag_enabled=False, node returns None without calling the LLM."""
    session = _make_session()
    llm = AsyncMock()
    llm.chat_completion = AsyncMock()

    collection = _make_collection(graph_rag_enabled=False)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state()
        result = await node_extract_entities(state, _make_config(session, llm))

    assert result == {"extracted_entities": None}
    # LLM must not have been called
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_entity_extraction_no_pii_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Chunk text content must never appear in log output."""
    session = _make_session()
    llm = AsyncMock()

    sensitive_text = "Patient John Doe has diabetes and takes metformin."
    chunks = [
        ChunkData(
            chunk_index=0,
            text=sensitive_text,
            page=1,
            section=None,
            token_count=12,
            point_id=_make_point_id(0),
        )
    ]

    entity_payload = json.dumps({"entities": []})
    relation_payload = json.dumps({"relations": []})
    llm.chat_completion = AsyncMock(
        side_effect=[
            _make_llm_response(entity_payload),
            _make_llm_response(relation_payload),
        ]
    )

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
        caplog.at_level(logging.DEBUG),
    ):
        state = _make_state(chunks=chunks)
        await node_extract_entities(state, _make_config(session, llm))

    # The actual chunk text (sensitive_text) must not appear anywhere in the captured logs
    for record in caplog.records:
        assert sensitive_text not in record.getMessage(), (
            f"Chunk text leaked into log: {record.getMessage()!r}"
        )
    # PII: patient name must not appear in logs
    assert "John Doe" not in caplog.text


@pytest.mark.asyncio
async def test_entity_extraction_empty_chunks_returns_none() -> None:
    """When state.chunks is empty, node returns None immediately."""
    session = _make_session()
    llm = AsyncMock()

    state = _make_state(chunks=[])

    result = await node_extract_entities(state, _make_config(session, llm))

    assert result == {"extracted_entities": None}
    llm.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_entity_extraction_relation_failure_is_nonfatal() -> None:
    """If the relation LLM call fails, entities are still returned."""
    session = _make_session()
    llm = AsyncMock()

    entity_payload = json.dumps(
        {
            "entities": [
                {
                    "type": "condition",
                    "name": "type 2 diabetes",
                    "icd_code": "E11",
                    "atc_code": None,
                    "confidence": 0.98,
                    "surface_forms": ["type 2 diabetes", "T2DM"],
                }
            ]
        }
    )

    call_count = 0

    async def _side_effect(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_llm_response(entity_payload)
        raise RuntimeError("relation LLM failed")

    llm.chat_completion = AsyncMock(side_effect=_side_effect)

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state()
        result = await node_extract_entities(state, _make_config(session, llm))

    # Should still return entities even though relation call failed
    assert result["extracted_entities"] is not None
    extracted = result["extracted_entities"]
    assert len(extracted) == 1
    assert extracted[0]["entities"][0]["name"] == "type 2 diabetes"
    assert extracted[0]["relations"] == []


@pytest.mark.asyncio
async def test_entity_extraction_invalid_entity_type_filtered() -> None:
    """Entities with an unrecognized type are silently filtered out."""
    session = _make_session()
    llm = AsyncMock()

    entity_payload = json.dumps(
        {
            "entities": [
                {
                    "type": "UNKNOWN_TYPE",
                    "name": "something",
                    "icd_code": None,
                    "atc_code": None,
                    "confidence": 0.5,
                    "surface_forms": ["something"],
                },
                {
                    "type": "drug",
                    "name": "aspirin",
                    "icd_code": None,
                    "atc_code": "B01AC06",
                    "confidence": 0.99,
                    "surface_forms": ["aspirin"],
                },
            ]
        }
    )
    relation_payload = json.dumps({"relations": []})
    llm.chat_completion = AsyncMock(
        side_effect=[
            _make_llm_response(entity_payload),
            _make_llm_response(relation_payload),
        ]
    )

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state()
        result = await node_extract_entities(state, _make_config(session, llm))

    assert result["extracted_entities"] is not None
    entities = result["extracted_entities"][0]["entities"]
    # Only the valid "drug" entity should survive
    assert len(entities) == 1
    assert entities[0]["name"] == "aspirin"


@pytest.mark.asyncio
async def test_entity_extraction_batch_partitioning() -> None:
    """With 7 chunks and BATCH_SIZE=5, the node makes entity calls for each of the 2 batches.

    BATCH_SIZE=5 → batch 0..4 (5 chunks) and batch 5..6 (2 chunks).
    Each batch always makes an entity-extraction LLM call.
    The relation-extraction call is only made if entities were found in that batch;
    when entities is empty the batch short-circuits after the entity call.
    """
    session = _make_session()
    llm = AsyncMock()

    # 7 chunks → 2 batches (5 + 2)
    chunks = [
        ChunkData(
            chunk_index=i,
            text=f"Chunk {i} content.",
            page=i,
            section=None,
            token_count=5,
            point_id=_make_point_id(i),
        )
        for i in range(7)
    ]

    # Empty entity responses: each batch makes only 1 LLM call (entity) → 2 total.
    # (Relation call is skipped when entity list is empty.)
    entity_payload = json.dumps({"entities": []})
    llm.chat_completion = AsyncMock(return_value=_make_llm_response(entity_payload))

    collection = _make_collection(graph_rag_enabled=True)
    model = _make_model()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_collection",
            new=AsyncMock(return_value=collection),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.get_model",
            new=AsyncMock(return_value=model),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities.update_step",
            new=AsyncMock(),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_extract_entities._load_prompt",
            return_value="prompt",
        ),
    ):
        state = _make_state(chunks=chunks)
        await node_extract_entities(state, _make_config(session, llm))

    # 2 batches × 1 entity call each = 2 total (relation call skipped on empty entity result)
    assert llm.chat_completion.call_count == 2
