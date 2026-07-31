"""Single point of Qdrant filter construction — mandatory tenant + RBAC filters.

NO other module may construct Qdrant filters directly.

Read vs. write filter distinction
----------------------------------
Public collections are readable by all tenants but writable only by the
managing tenant (managed_by_tenant_id). Two separate builder functions enforce
this at the filter level:

  build_read_filter()  — used for search queries.
      Allows: own tenant_id + allowed_collection_ids  OR  public_collection_ids.

  build_mandatory_filter()  — legacy alias kept for call sites that only deal
      with private collections (no public collections in scope). Raises
      EmptyCollectionListError if collection_ids is empty — same behaviour as before.

  build_write_filter()  — used internally by upsert/delete operations.
      Restricts: tenant_id == caller_tenant_id only.
      Note: the "is the caller allowed to write a public collection" check is done
      in RetrievalService (Python layer), not in the Qdrant filter, because Qdrant
      filters cannot express "if is_public then require managed_by_tenant_id".
"""

from __future__ import annotations

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from src.retrieval.exceptions import EmptyCollectionListError


def build_read_filter(
    tenant_id: str,
    allowed_collection_ids: list[str],
    public_collection_ids: list[str],
) -> Filter:
    """Build the read filter for search queries — private + public collections.

    Logic (SQL equivalent):
        (payload.tenant_id = :tenant_id AND payload.collection_id IN :allowed_ids)
        OR
        (payload.collection_id IN :public_ids)

    At least one of the two lists must be non-empty, otherwise the query would
    scan the entire Qdrant shard — which is forbidden.

    Args:
        tenant_id: Caller's tenant UUID as string.
        allowed_collection_ids: Private collection IDs the caller may read.
        public_collection_ids: Platform-wide public collection IDs.

    Returns:
        Qdrant Filter with a SHOULD clause (OR semantics) between the private
        branch and the public branch.

    Raises:
        EmptyCollectionListError: If both lists are empty.
    """
    if not allowed_collection_ids and not public_collection_ids:
        raise EmptyCollectionListError(
            "search requires at least one allowed collection; "
            "user has no private or public collection access"
        )

    should_clauses: list[Filter] = []

    if allowed_collection_ids:
        # Private branch: must match tenant_id AND collection_id.
        should_clauses.append(
            Filter(
                must=[
                    FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
                    FieldCondition(key="collection_id", match=MatchAny(any=allowed_collection_ids)),
                ]
            )
        )

    if public_collection_ids:
        # Public branch: collection_id match is sufficient — no tenant_id constraint
        # because the data in public collections carries the managing tenant's tenant_id
        # in the payload, not the calling tenant's tenant_id.
        should_clauses.append(
            Filter(
                must=[
                    FieldCondition(key="collection_id", match=MatchAny(any=public_collection_ids)),
                ]
            )
        )

    if len(should_clauses) == 1:
        # Optimisation: a single branch can be a plain must filter (avoids SHOULD overhead).
        return should_clauses[0]

    return Filter(should=should_clauses)


def build_mandatory_filter(
    tenant_id: str,
    collection_ids: list[str],
) -> Filter:
    """Build mandatory tenant+RBAC filter for private-only search queries.

    Retained for backwards compatibility. Call sites that do not deal with
    public collections should continue to use this function.

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


def build_write_filter(
    tenant_id: str,
    document_id: str,
) -> Filter:
    """Build filter for write operations (upsert / delete) scoped to one tenant + document.

    This filter is intentionally stricter than the read filter — it NEVER
    includes public collection IDs so that a non-owner tenant cannot accidentally
    delete public chunks. The public-collection ownership check happens in
    RetrievalService before this filter is applied.

    Args:
        tenant_id: The writing tenant's UUID as string.
        document_id: The document whose chunks to affect.

    Returns:
        Filter matching only points owned by tenant_id for document_id.
    """
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            FieldCondition(key="document_id", match=MatchValue(value=document_id)),
        ]
    )


def build_document_delete_filter(tenant_id: str, document_id: str) -> Filter:
    """Build filter for deleting all chunks of one document within a tenant.

    Alias of build_write_filter for readability at call sites.
    """
    return build_write_filter(tenant_id, document_id)


def build_tenant_delete_filter(tenant_id: str) -> Filter:
    """Build filter for deleting ALL points for a tenant. Use only from DeletionService."""
    return Filter(
        must=[
            FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
        ]
    )


def merge_filters(mandatory: Filter, additional: Filter | None) -> Filter:
    """Extend mandatory filter with additional conditions. Never replaces mandatory.

    The mandatory filter is always preserved in full — it is never weakened or
    dropped.  Two structural cases are handled:

    1. mandatory uses MUST (normal private-only case):
       Merge both must lists into a single flat must list.

    2. mandatory uses SHOULD (OR semantics — mixed private + public collections):
       The SHOULD branches cannot be flattened into a must list without changing
       semantics.  Instead the mandatory filter is nested as a sub-filter so that
       the final filter reads:
           must=[Filter(should=[private_branch, public_branch]), additional]
       which translates to: "(private OR public) AND additional".

    Args:
        mandatory: The required tenant+RBAC filter (output of build_read_filter
            or build_mandatory_filter). Always included and never weakened.
        additional: Optional extra filter to AND on top of the mandatory filter.

    Returns:
        Merged Filter with combined conditions that still enforces tenant isolation.
    """
    if additional is None:
        return mandatory

    if mandatory.should is not None:
        # mandatory is a SHOULD (OR) filter — preserving it requires nesting it
        # as a sub-filter so that `additional` can be ANDed alongside it.
        # Result: must=[Filter(should=[...]), additional]
        # Semantics: "(private_branch OR public_branch) AND additional_conditions"
        return Filter(
            must=[Filter(should=mandatory.should), additional],
        )

    # Normal case: mandatory is a MUST filter — merge both must lists flat.
    merged_must = list(mandatory.must or []) + list(additional.must or [])
    return Filter(must=merged_must)
