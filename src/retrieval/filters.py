"""Single point of Qdrant filter construction — mandatory tenant + RBAC filters.

NO other module may construct Qdrant filters directly.
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from src.retrieval.exceptions import EmptyCollectionListError


def build_mandatory_filter(
    tenant_id: str,
    collection_ids: list[str],
) -> Filter:
    """Build mandatory tenant+RBAC filter for every Qdrant search query.

    Args:
        tenant_id: Tenant UUID as string.
        collection_ids: Collection UUIDs the user is allowed to access.

    Returns:
        Filter with MUST conditions: tenant_id match AND collection_id in list.

    Raises:
        EmptyCollectionListError: If collection_ids is empty — prevents full-tenant scan.
    """
    if not collection_ids:
        raise EmptyCollectionListError(
            "search requires at least one allowed collection; user has no collection access"
        )
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="collection_id", match=MatchAny(any=collection_ids)),
        ]
    )


def build_document_delete_filter(tenant_id: str, document_id: str) -> Filter:
    """Build filter for deleting all chunks of one document within a tenant."""
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="document_id", match=MatchValue(value=document_id)),
        ]
    )


def build_tenant_delete_filter(tenant_id: str) -> Filter:
    """Build filter for deleting ALL points for a tenant. Use only from DeletionService."""
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
        ]
    )


def merge_filters(mandatory: Filter, additional: Filter | None) -> Filter:
    """Extend mandatory filter with additional conditions. Never replaces mandatory.

    Args:
        mandatory: The required tenant+RBAC filter. Always included.
        additional: Optional extra filter to merge into must conditions.

    Returns:
        Merged Filter with combined must conditions.
    """
    if additional is None:
        return mandatory
    merged_must = list(mandatory.must or []) + list(additional.must or [])
    return Filter(must=merged_must)
