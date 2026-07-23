"""Retrieval layer exceptions."""


class RetrievalError(Exception):
    """Base exception for retrieval layer."""


class EmptyCollectionListError(RetrievalError):
    """Raised when search is attempted with empty allowed_collection_ids.

    An empty list would cause Qdrant to scan across all tenants' data — forbidden.
    """


class QdrantUnavailableError(RetrievalError):
    """Raised when all retry attempts to Qdrant are exhausted."""
