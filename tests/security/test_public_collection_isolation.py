"""Security tests for public collection isolation.

Verifies three core invariants:
  1. Public collections are readable by any tenant (cross-tenant read is intentional).
  2. Public collections are NOT writable by tenants that do not manage them.
  3. Private collections are never readable by a different tenant.

All tests are pure-unit (no DB, no Qdrant) — they exercise the Python-layer
enforcement in RetrievalService and the filter builders.

Mark: @pytest.mark.tenant_isolation
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.retrieval.exceptions import EmptyCollectionListError
from src.retrieval.filters import build_read_filter, merge_filters
from src.retrieval.schemas import QdrantPoint, TenantContext
from src.retrieval.service import RetrievalService, _assert_write_allowed_for_public

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()

PUBLIC_COLLECTION = uuid.uuid4()  # managed by TENANT_A
PRIVATE_COLLECTION_A = uuid.uuid4()  # owned by TENANT_A
PRIVATE_COLLECTION_B = uuid.uuid4()  # owned by TENANT_B


def make_ctx(
    tenant_id: uuid.UUID,
    allowed: list[uuid.UUID] | None = None,
    public: list[uuid.UUID] | None = None,
) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        allowed_collection_ids=allowed or [],
        public_collection_ids=public or [],
    )


def make_point(tenant_id: uuid.UUID, collection_id: uuid.UUID) -> QdrantPoint:
    return QdrantPoint(
        id=uuid.uuid4(),
        vector=[0.1, 0.2, 0.3],
        payload={
            "tenant_id": str(tenant_id),
            "collection_id": str(collection_id),
            "document_id": str(uuid.uuid4()),
            "text": "sample",
        },
    )


def _make_retrieval_service() -> RetrievalService:
    client = MagicMock()
    client.query_points = AsyncMock()
    client.upsert = AsyncMock()
    client.delete = AsyncMock()
    return RetrievalService(client=client)


# ---------------------------------------------------------------------------
# Filter-level tests (pure Python, no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestReadFilterPublicCollections:
    """build_read_filter correctly includes public collections."""

    def test_public_only_context_builds_collection_id_filter(self) -> None:
        """A tenant with only public collections gets a filter on collection_id only."""
        flt = build_read_filter(
            tenant_id=str(TENANT_B),
            allowed_collection_ids=[],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        # Single branch → plain must filter (not SHOULD)
        assert flt.must is not None
        conditions = {c.key for c in flt.must}  # type: ignore[union-attr]
        assert "collection_id" in conditions
        # MUST NOT contain tenant_id — public reads cross tenant boundary intentionally.
        assert "tenant_id" not in conditions

    def test_mixed_context_builds_should_filter(self) -> None:
        """Private + public collections produce an OR (SHOULD) filter."""
        flt = build_read_filter(
            tenant_id=str(TENANT_A),
            allowed_collection_ids=[str(PRIVATE_COLLECTION_A)],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        assert flt.should is not None
        assert len(flt.should) == 2  # type: ignore[arg-type]

    def test_private_only_context_filters_by_tenant_and_collection(self) -> None:
        """Private-only context: tenant_id AND collection_id must both match."""
        flt = build_read_filter(
            tenant_id=str(TENANT_A),
            allowed_collection_ids=[str(PRIVATE_COLLECTION_A)],
            public_collection_ids=[],
        )
        assert flt.must is not None
        condition_keys = {c.key for c in flt.must}  # type: ignore[union-attr]
        assert "tenant_id" in condition_keys
        assert "collection_id" in condition_keys

    def test_empty_both_lists_raises(self) -> None:
        """Empty allowed + empty public is always forbidden — would scan all data."""
        with pytest.raises(EmptyCollectionListError):
            build_read_filter(
                tenant_id=str(TENANT_A),
                allowed_collection_ids=[],
                public_collection_ids=[],
            )

    def test_private_collection_not_accessible_via_public_filter(self) -> None:
        """A private collection not in public_collection_ids is excluded from the filter."""
        flt = build_read_filter(
            tenant_id=str(TENANT_B),
            allowed_collection_ids=[],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        # The private collection of TENANT_A must not appear anywhere in the filter.
        import json

        filter_json = json.dumps(flt.model_dump())
        assert str(PRIVATE_COLLECTION_A) not in filter_json


# ---------------------------------------------------------------------------
# Write guard: _assert_write_allowed_for_public
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestWriteGuardFunction:
    """_assert_write_allowed_for_public enforces ownership on public collections."""

    def test_non_owner_cannot_write_public_collection(self) -> None:
        """Tenant B cannot write to a public collection managed by Tenant A."""
        ctx = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B],
            public=[PUBLIC_COLLECTION],
        )
        with pytest.raises(PermissionError, match="does not own public collection"):
            _assert_write_allowed_for_public(ctx, PUBLIC_COLLECTION)

    def test_owner_can_write_public_collection(self) -> None:
        """Tenant A (managing tenant) may write to its public collection."""
        ctx = make_ctx(
            tenant_id=TENANT_A,
            # The managing tenant has the public collection in allowed_collection_ids.
            allowed=[PUBLIC_COLLECTION, PRIVATE_COLLECTION_A],
            public=[PUBLIC_COLLECTION],
        )
        # Must not raise.
        _assert_write_allowed_for_public(ctx, PUBLIC_COLLECTION)

    def test_none_collection_id_is_skipped(self) -> None:
        """None collection_id skips the guard — used when collection context is unknown."""
        ctx = make_ctx(tenant_id=TENANT_B, public=[PUBLIC_COLLECTION])
        # Must not raise.
        _assert_write_allowed_for_public(ctx, None)

    def test_private_collection_not_in_public_list_is_allowed(self) -> None:
        """Writing to a private collection (not in public_collection_ids) is always permitted."""
        ctx = make_ctx(
            tenant_id=TENANT_A,
            allowed=[PRIVATE_COLLECTION_A],
            public=[PUBLIC_COLLECTION],
        )
        # PRIVATE_COLLECTION_A is not in public_collection_ids → guard is silent.
        _assert_write_allowed_for_public(ctx, PRIVATE_COLLECTION_A)


# ---------------------------------------------------------------------------
# RetrievalService integration — upsert_batch
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestPublicCollectionNotWritableByNonOwner:
    """RetrievalService.upsert_batch rejects non-owner writes to public collections."""

    @pytest.mark.asyncio
    async def test_public_collection_not_writable_by_non_owner_tenant(self) -> None:
        """Tenant B upsert into a public collection (owned by A) → PermissionError."""
        service = _make_retrieval_service()
        ctx = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B],
            public=[PUBLIC_COLLECTION],
        )
        # The point claims tenant_id=TENANT_B but targets PUBLIC_COLLECTION.
        point = make_point(TENANT_B, PUBLIC_COLLECTION)

        with pytest.raises(PermissionError, match="does not own public collection"):
            await service.upsert_batch(ctx, "emb_test", [point])

        # Qdrant must never be called.
        service._client.upsert.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_owner_tenant_can_upsert_to_public_collection(self) -> None:
        """Tenant A (managing tenant) can upsert into its public collection."""
        service = _make_retrieval_service()
        service._client.upsert = AsyncMock(return_value=None)
        ctx = make_ctx(
            tenant_id=TENANT_A,
            allowed=[PUBLIC_COLLECTION, PRIVATE_COLLECTION_A],
            public=[PUBLIC_COLLECTION],
        )
        point = make_point(TENANT_A, PUBLIC_COLLECTION)

        # Must not raise.
        await service.upsert_batch(ctx, "emb_test", [point])

        service._client.upsert.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_non_owner_cannot_spoof_tenant_id_to_write_public(self) -> None:
        """Even with matching tenant_id payload, non-owner is blocked by collection check."""
        service = _make_retrieval_service()
        ctx = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B],
            public=[PUBLIC_COLLECTION],
        )
        # Attacker spoofs tenant_id to be TENANT_B (they own it) but targets PUBLIC_COLLECTION.
        # The collection-level guard must fire BEFORE the payload tenant_id check.
        point = make_point(TENANT_B, PUBLIC_COLLECTION)

        with pytest.raises(PermissionError):
            await service.upsert_batch(ctx, "emb_test", [point])

        service._client.upsert.assert_not_called()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RetrievalService integration — delete_by_document
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestPublicCollectionDeleteIsolation:
    """RetrievalService.delete_by_document rejects non-owner deletes on public collections."""

    @pytest.mark.asyncio
    async def test_non_owner_cannot_delete_from_public_collection(self) -> None:
        """Tenant B cannot delete documents from a public collection managed by A."""
        service = _make_retrieval_service()
        ctx = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B],
            public=[PUBLIC_COLLECTION],
        )
        with pytest.raises(PermissionError, match="does not own public collection"):
            await service.delete_by_document(
                ctx,
                "emb_test",
                uuid.uuid4(),
                collection_id=PUBLIC_COLLECTION,
            )

        service._client.delete.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_owner_can_delete_from_public_collection(self) -> None:
        """Tenant A (managing tenant) can delete documents from its public collection."""
        service = _make_retrieval_service()
        mock_result = MagicMock()
        mock_result.result.count = 5
        service._client.delete = AsyncMock(return_value=mock_result)

        ctx = make_ctx(
            tenant_id=TENANT_A,
            allowed=[PUBLIC_COLLECTION, PRIVATE_COLLECTION_A],
            public=[PUBLIC_COLLECTION],
        )
        count = await service.delete_by_document(
            ctx,
            "emb_test",
            uuid.uuid4(),
            collection_id=PUBLIC_COLLECTION,
        )
        assert count == 5


# ---------------------------------------------------------------------------
# Private collection cross-tenant read isolation
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestPrivateCollectionNotReadableByOtherTenant:
    """Private collections are never accessible by other tenants at the filter level."""

    def test_private_collection_of_b_not_in_tenant_a_read_filter(self) -> None:
        """Tenant A's read filter cannot include Tenant B's private collection."""
        flt = build_read_filter(
            tenant_id=str(TENANT_A),
            allowed_collection_ids=[str(PRIVATE_COLLECTION_A)],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        import json

        filter_json = json.dumps(flt.model_dump())
        assert str(PRIVATE_COLLECTION_B) not in filter_json

    def test_tenant_b_cannot_add_private_collection_a_to_context(self) -> None:
        """Scenario: Tenant B incorrectly receives Tenant A's private collection ID.

        The read filter produced for Tenant B MUST also include tenant_id=TENANT_B
        so that even if PRIVATE_COLLECTION_A snuck into allowed_collection_ids,
        the filter would only return Tenant B's own data for that collection ID
        (which in practice is empty, because tenant_id in the payload would be TENANT_A).
        """
        # Worst case: attacker injects PRIVATE_COLLECTION_A into their allowed list.
        ctx_b_compromised = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B, PRIVATE_COLLECTION_A],  # compromised!
            public=[PUBLIC_COLLECTION],
        )
        flt = build_read_filter(
            tenant_id=str(ctx_b_compromised.tenant_id),
            allowed_collection_ids=[str(c) for c in ctx_b_compromised.allowed_collection_ids],
            public_collection_ids=[str(c) for c in ctx_b_compromised.public_collection_ids],
        )
        # Private branch always includes tenant_id=TENANT_B — even if PRIVATE_COLLECTION_A
        # somehow ends up in the list, it will only match points that also have
        # tenant_id=TENANT_B, which means zero results from Tenant A's data.
        import json

        filter_json = json.dumps(flt.model_dump())
        # tenant_id of the caller is present in the private branch
        assert str(TENANT_B) in filter_json
        # tenant_id of A must NOT appear anywhere in the filter
        assert str(TENANT_A) not in filter_json

    @pytest.mark.asyncio
    async def test_public_collection_readable_by_any_tenant(self) -> None:
        """Both Tenant A and Tenant B can search public collections (read is allowed)."""
        service = _make_retrieval_service()

        # Simulate a Qdrant hit from a public collection
        mock_point = MagicMock()
        mock_point.id = str(uuid.uuid4())
        mock_point.score = 0.95
        mock_point.payload = {
            "tenant_id": str(TENANT_A),  # managing tenant
            "collection_id": str(PUBLIC_COLLECTION),
            "document_id": str(uuid.uuid4()),
            "text": "ICD-11 clinical protocol",
        }
        mock_response = MagicMock()
        mock_response.points = [mock_point]
        service._client.query_points = AsyncMock(return_value=mock_response)

        # Tenant B (non-owner) context with public collection in scope
        ctx_b = make_ctx(
            tenant_id=TENANT_B,
            allowed=[PRIVATE_COLLECTION_B],
            public=[PUBLIC_COLLECTION],
        )

        results = await service.search(
            ctx=ctx_b,
            qdrant_collection="emb_test",
            query_vector=[0.1, 0.2, 0.3],
            top_k=5,
        )

        # Search must succeed and return the public collection result
        assert len(results) == 1
        assert results[0].collection_id == PUBLIC_COLLECTION
        service._client.query_points.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# merge_filters correctness: SHOULD (OR) mandatory filter
# ---------------------------------------------------------------------------


@pytest.mark.tenant_isolation
class TestMergeFiltersWithShouldMandatory:
    """merge_filters must not drop SHOULD branches when mandatory is an OR filter.

    Regression test for the silent-strip bug: when build_read_filter returns a
    Filter(should=[...]) (mixed private + public context), the old merge_filters
    read `mandatory.must` which was None and produced Filter(must=additional.must)
    — completely discarding the tenant+collection isolation clauses.
    """

    def test_merge_filters_with_should_filter_preserves_tenant_isolation(self) -> None:
        """SHOULD branches survive merge_filters — tenant isolation clauses are kept."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        # Build a mixed-context mandatory filter (has SHOULD, not MUST at top level).
        mandatory = build_read_filter(
            tenant_id=str(TENANT_A),
            allowed_collection_ids=[str(PRIVATE_COLLECTION_A)],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        assert mandatory.should is not None, "pre-condition: mandatory must use SHOULD"

        additional = Filter(must=[FieldCondition(key="language", match=MatchValue(value="pl"))])

        merged = merge_filters(mandatory, additional)

        # Top-level structure: must=[nested_should_filter, additional_filter]
        assert merged.must is not None, "merged filter must have a must list"
        assert len(merged.must) == 2, "must list must contain exactly two sub-filters"

        # First element must re-wrap the SHOULD branches (not lose them).
        nested = merged.must[0]
        assert hasattr(nested, "should") and nested.should is not None, (
            "first must element must be a sub-filter that preserves SHOULD branches"
        )
        assert len(nested.should) == 2, "both SHOULD branches (private + public) must be present"

        # Confirm tenant_id still appears somewhere inside the nested should branches.
        import json

        merged_json = json.dumps(merged.model_dump())
        assert str(TENANT_A) in merged_json, "tenant_id must still be present after merge"

        # Second element is the additional filter.
        extra = merged.must[1]
        assert hasattr(extra, "must") and extra.must is not None
        assert extra.must[0].key == "language"  # type: ignore[union-attr]

    def test_merge_filters_with_additional_filter_does_not_expose_cross_tenant_data(
        self,
    ) -> None:
        """After merge_filters, the filter still prevents cross-tenant data leakage.

        Scenario: TENANT_B builds a mixed-context read filter (private + public).
        An additional score-threshold filter is merged in.  The result must still
        carry TENANT_B's tenant_id in the private SHOULD branch, and must NOT
        contain TENANT_A's tenant_id.
        """
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        mandatory = build_read_filter(
            tenant_id=str(TENANT_B),
            allowed_collection_ids=[str(PRIVATE_COLLECTION_B)],
            public_collection_ids=[str(PUBLIC_COLLECTION)],
        )
        assert mandatory.should is not None, "pre-condition: mandatory must use SHOULD"

        additional = Filter(
            must=[FieldCondition(key="doc_type", match=MatchValue(value="clinical_note"))]
        )

        merged = merge_filters(mandatory, additional)

        import json

        merged_json = json.dumps(merged.model_dump())

        # Tenant B's isolation must survive the merge.
        assert str(TENANT_B) in merged_json, "TENANT_B's tenant_id must remain in the merged filter"

        # Tenant A's ID must never leak into TENANT_B's filter.
        assert str(TENANT_A) not in merged_json, (
            "TENANT_A's tenant_id must not appear in TENANT_B's merged filter"
        )

        # The additional condition must also be present.
        assert "doc_type" in merged_json, "additional filter conditions must survive the merge"
        assert "clinical_note" in merged_json
