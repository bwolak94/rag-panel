"""Unit tests for ChunkConfig and ValidationConfig Pydantic validators."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.schemas.collection import ChunkConfig, ValidationConfig


class TestChunkConfigValidation:
    def test_valid_defaults(self) -> None:
        cfg = ChunkConfig()
        assert cfg.chunk_size == 512
        assert cfg.overlap == 64
        assert cfg.strategy == "recursive"

    def test_valid_custom_values(self) -> None:
        cfg = ChunkConfig(chunk_size=512, overlap=64)
        assert cfg.chunk_size == 512
        assert cfg.overlap == 64

    def test_minimum_valid(self) -> None:
        """chunk_size=64, overlap=0 — boundary values are accepted."""
        cfg = ChunkConfig(chunk_size=64, overlap=0)
        assert cfg.chunk_size == 64
        assert cfg.overlap == 0

    def test_overlap_equals_chunk_size_rejected(self) -> None:
        """overlap >= chunk_size → ValidationError."""
        with pytest.raises(ValidationError, match="overlap must be less than chunk_size"):
            ChunkConfig(chunk_size=100, overlap=100)

    def test_overlap_greater_than_chunk_size_rejected(self) -> None:
        with pytest.raises(ValidationError, match="overlap must be less than chunk_size"):
            ChunkConfig(chunk_size=100, overlap=150)

    def test_chunk_size_exceeds_max(self) -> None:
        """chunk_size > 4096 → ValidationError."""
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_size=5000)

    def test_chunk_size_below_min(self) -> None:
        """chunk_size < 64 → ValidationError."""
        with pytest.raises(ValidationError):
            ChunkConfig(chunk_size=32)

    def test_invalid_strategy(self) -> None:
        with pytest.raises(ValidationError):
            ChunkConfig(strategy="invalid")  # type: ignore[arg-type]

    def test_all_valid_strategies(self) -> None:
        for strategy in ("recursive", "sentence", "semantic", "by_section"):
            cfg = ChunkConfig(strategy=strategy)  # type: ignore[arg-type]
            assert cfg.strategy == strategy

    def test_document_type_overrides_nested_chunk_config(self) -> None:
        """document_type_overrides can contain nested ChunkConfig entries."""
        cfg = ChunkConfig(
            chunk_size=512,
            overlap=64,
            document_type_overrides={
                "pdf": ChunkConfig(chunk_size=256, overlap=32),
            },
        )
        assert cfg.document_type_overrides["pdf"].chunk_size == 256

    def test_document_type_overrides_nested_invalid(self) -> None:
        """Nested ChunkConfig inside overrides is also validated."""
        with pytest.raises(ValidationError):
            ChunkConfig(
                chunk_size=512,
                overlap=64,
                document_type_overrides={
                    "pdf": {"chunk_size": 200, "overlap": 200},  # overlap >= chunk_size
                },
            )


class TestValidationConfigValidation:
    def test_valid_defaults(self) -> None:
        cfg = ValidationConfig()
        assert cfg.confidence_threshold == 0.7
        assert cfg.pii_action == "flag"
        assert cfg.require_review is False

    def test_pii_action_block(self) -> None:
        cfg = ValidationConfig(pii_action="block")
        assert cfg.pii_action == "block"

    def test_all_valid_pii_actions(self) -> None:
        for action in ("block", "flag", "allow", "review"):
            cfg = ValidationConfig(pii_action=action)  # type: ignore[arg-type]
            assert cfg.pii_action == action

    def test_invalid_pii_action(self) -> None:
        with pytest.raises(ValidationError):
            ValidationConfig(pii_action="ignore")  # type: ignore[arg-type]

    def test_confidence_threshold_bounds(self) -> None:
        ValidationConfig(confidence_threshold=0.0)
        ValidationConfig(confidence_threshold=1.0)
        with pytest.raises(ValidationError):
            ValidationConfig(confidence_threshold=1.1)
        with pytest.raises(ValidationError):
            ValidationConfig(confidence_threshold=-0.1)
