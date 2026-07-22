"""Unit tests for node_pii_scan."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.graphs.ingest_graph.nodes.node_pii_scan import DefaultPIIScanner, node_pii_scan
from src.graphs.ingest_graph.state import IngestState, ValidationResult

TENANT_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
COLLECTION_ID = uuid.uuid4()
JOB_ID = uuid.uuid4()


def _make_state(**kwargs: object) -> IngestState:
    return IngestState(
        document_id=DOCUMENT_ID,
        tenant_id=TENANT_ID,
        collection_id=COLLECTION_ID,
        minio_key=f"raw/{COLLECTION_ID}/{DOCUMENT_ID}/doc.pdf",
        job_id=JOB_ID,
        extracted_text="Sample document text without PII.",
        **kwargs,
    )


def _make_collection(pii_action: str = "review") -> MagicMock:
    coll = MagicMock()
    coll.validation_config = {"pii_action": pii_action}
    return coll


def _make_config(session: object) -> dict:
    return {"configurable": {"db": session}}


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock())
    session.commit = AsyncMock()
    return session


class _NoPIIScanner:
    async def scan(self, text: str) -> tuple[bool, list[str]]:
        return False, []


class _PIIScanner:
    async def scan(self, text: str) -> tuple[bool, list[str]]:
        return True, ["PERSON", "PESEL"]


@pytest.mark.asyncio
async def test_no_pii_returns_empty() -> None:
    """No PII detected → return {} with status unchanged."""
    session = _make_session()
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_pii_scan(state, _make_config(session), scanner=_NoPIIScanner())

    assert result == {}


@pytest.mark.asyncio
async def test_pii_detected_returns_needs_review() -> None:
    """PII detected with pii_action=review → {"status": "needs_review"}."""
    session = _make_session()
    state = _make_state()

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.get_collection",
            new=AsyncMock(return_value=_make_collection("review")),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.update_step",
            new=AsyncMock(),
        ),
    ):
        result = await node_pii_scan(state, _make_config(session), scanner=_PIIScanner())

    assert result["status"] == "needs_review"
    assert isinstance(result["validation_result"], ValidationResult)
    assert "PERSON" in result["validation_result"].pii_flags
    assert "PESEL" in result["validation_result"].pii_flags


@pytest.mark.asyncio
async def test_pii_count_logged_not_values() -> None:
    """update_step meta must contain flags_count (int), not actual PII strings."""
    session = _make_session()
    state = _make_state()
    captured_meta: dict = {}

    async def capture_step(*args: object, **kwargs: object) -> None:
        captured_meta.update(kwargs.get("meta") or {})

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.get_collection",
            new=AsyncMock(return_value=_make_collection("review")),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.update_step",
            new=capture_step,
        ),
    ):
        await node_pii_scan(state, _make_config(session), scanner=_PIIScanner())

    assert "flags_count" in captured_meta
    assert isinstance(captured_meta["flags_count"], int)
    # pii flag values must not appear in meta
    for v in captured_meta.values():
        if isinstance(v, list):
            assert v == [], "PII type labels must not be in step meta"


@pytest.mark.asyncio
async def test_scanner_injectable() -> None:
    """Custom scanner is used; DefaultPIIScanner is NOT called."""
    session = _make_session()
    state = _make_state()
    custom_called = False

    class CustomScanner:
        async def scan(self, text: str) -> tuple[bool, list[str]]:
            nonlocal custom_called
            custom_called = True
            return False, []

    with (
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.get_collection",
            new=AsyncMock(return_value=_make_collection()),
        ),
        patch(
            "src.graphs.ingest_graph.nodes.node_pii_scan.update_step",
            new=AsyncMock(),
        ),
        patch.object(
            DefaultPIIScanner,
            "scan",
            new=AsyncMock(side_effect=AssertionError("DefaultPIIScanner must not be called")),
        ),
    ):
        await node_pii_scan(state, _make_config(session), scanner=CustomScanner())

    assert custom_called


@pytest.mark.asyncio
async def test_default_scanner_detects_pesel() -> None:
    """DefaultPIIScanner regex finds an 11-digit PESEL."""
    scanner = DefaultPIIScanner()
    pii_detected, labels = await scanner.scan("Patient PESEL: 90020512345")
    assert pii_detected is True
    assert "PESEL" in labels
