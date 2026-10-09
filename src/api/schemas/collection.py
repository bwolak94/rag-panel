"""Pydantic schemas for the Collections API — re-exported from domain layer."""

from src.domain.schemas.collection import ChunkConfig as ChunkConfig
from src.domain.schemas.collection import ChunkStrategyConfig as ChunkStrategyConfig
from src.domain.schemas.collection import CollectionCreate as CollectionCreate
from src.domain.schemas.collection import CollectionListResponse as CollectionListResponse
from src.domain.schemas.collection import CollectionResponse as CollectionResponse
from src.domain.schemas.collection import CollectionUpdate as CollectionUpdate
from src.domain.schemas.collection import EmbeddingModelRef as EmbeddingModelRef
from src.domain.schemas.collection import LegacyChunkSizeOverride as LegacyChunkSizeOverride
from src.domain.schemas.collection import ValidationConfig as ValidationConfig
