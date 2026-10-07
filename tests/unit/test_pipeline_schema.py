"""Unit tests for pipeline Pydantic schemas — ABTestConfig and PromptConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.domain.schemas.pipeline import ABTestConfig, PromptConfig


class TestABTestConfig:
    """Validation tests for ABTestConfig."""

    def test_traffic_split_above_max_raises(self) -> None:
        """traffic_split > 1.0 must fail validation."""
        with pytest.raises(ValidationError):
            ABTestConfig(traffic_split=1.5)

    def test_traffic_split_below_min_raises(self) -> None:
        """traffic_split < 0.0 must fail validation."""
        with pytest.raises(ValidationError):
            ABTestConfig(traffic_split=-0.1)

    def test_valid_config_is_accepted(self) -> None:
        """Valid ABTestConfig with enabled=True and all required fields must be accepted."""
        cfg = ABTestConfig(
            enabled=True,
            shadow_prompt_version="v2",
            traffic_split=0.5,
            experiment_id="exp-v2",
        )
        assert cfg.enabled is True
        assert cfg.traffic_split == 0.5
        assert cfg.experiment_id == "exp-v2"

    def test_defaults_are_applied(self) -> None:
        """Default values must match the spec."""
        cfg = ABTestConfig()
        assert cfg.enabled is False
        assert cfg.shadow_prompt_version == ""
        assert cfg.traffic_split == 0.1
        assert cfg.experiment_id == ""

    def test_shadow_prompt_version_too_long_raises(self) -> None:
        """shadow_prompt_version longer than 32 chars must fail."""
        with pytest.raises(ValidationError):
            ABTestConfig(shadow_prompt_version="v" * 33)

    def test_experiment_id_too_long_raises(self) -> None:
        """experiment_id longer than 128 chars must fail."""
        with pytest.raises(ValidationError):
            ABTestConfig(experiment_id="x" * 129)

    def test_traffic_split_boundary_zero_is_valid(self) -> None:
        """traffic_split=0.0 is a valid boundary value."""
        cfg = ABTestConfig(traffic_split=0.0)
        assert cfg.traffic_split == 0.0

    def test_traffic_split_boundary_one_is_valid(self) -> None:
        """traffic_split=1.0 is a valid boundary value."""
        cfg = ABTestConfig(traffic_split=1.0)
        assert cfg.traffic_split == 1.0


class TestPromptConfigWithABTest:
    """Tests for PromptConfig with the nested ab_test field."""

    def test_prompt_config_default_has_no_ab_test(self) -> None:
        """Default PromptConfig must have ab_test=None."""
        cfg = PromptConfig()
        assert cfg.ab_test is None

    def test_prompt_config_accepts_valid_ab_test(self) -> None:
        """PromptConfig must accept a valid ABTestConfig nested object."""
        cfg = PromptConfig(
            ab_test=ABTestConfig(
                enabled=True,
                shadow_prompt_version="v2",
                traffic_split=0.2,
                experiment_id="exp-001",
            )
        )
        assert cfg.ab_test is not None
        assert cfg.ab_test.enabled is True
        assert cfg.ab_test.traffic_split == 0.2

    def test_prompt_config_round_trip(self) -> None:
        """PromptConfig round-trip through model_dump() and model_validate() must be stable."""
        original = PromptConfig(
            temperature=0.7,
            max_tokens=512,
            ab_test=ABTestConfig(
                enabled=True,
                shadow_prompt_version="v3",
                traffic_split=0.25,
                experiment_id="round-trip-test",
            ),
        )
        dumped = original.model_dump()
        restored = PromptConfig.model_validate(dumped)

        assert restored.temperature == original.temperature
        assert restored.max_tokens == original.max_tokens
        assert restored.ab_test is not None
        assert restored.ab_test.enabled == original.ab_test.enabled  # type: ignore[union-attr]
        assert restored.ab_test.shadow_prompt_version == original.ab_test.shadow_prompt_version  # type: ignore[union-attr]
        assert restored.ab_test.traffic_split == original.ab_test.traffic_split  # type: ignore[union-attr]
        assert restored.ab_test.experiment_id == original.ab_test.experiment_id  # type: ignore[union-attr]

    def test_prompt_config_with_none_ab_test_explicit(self) -> None:
        """Explicitly passing ab_test=None must remain None after round-trip."""
        cfg = PromptConfig(ab_test=None)
        assert cfg.ab_test is None
        dumped = cfg.model_dump()
        restored = PromptConfig.model_validate(dumped)
        assert restored.ab_test is None

    def test_prompt_config_invalid_ab_test_traffic_split_propagates(self) -> None:
        """Invalid traffic_split inside ab_test must raise ValidationError on PromptConfig."""
        with pytest.raises(ValidationError):
            PromptConfig(
                ab_test=ABTestConfig.model_validate({"enabled": False, "traffic_split": 2.0})
            )

    def test_ab_test_empty_shadow_version_rejected(self) -> None:
        """empty shadow_prompt_version when enabled=True must raise ValidationError."""
        with pytest.raises(ValidationError):
            ABTestConfig(
                enabled=True,
                shadow_prompt_version="",
                experiment_id="exp-1",
                traffic_split=0.5,
            )

    def test_ab_test_disabled_empty_fields_valid(self) -> None:
        """enabled=False with empty shadow_prompt_version and experiment_id must be valid."""
        config = ABTestConfig(enabled=False, shadow_prompt_version="", experiment_id="")
        assert not config.enabled


class TestPromptConfigMaxContextTokens:
    """Tests for PromptConfig.max_context_tokens (ADR-021)."""

    def test_default_max_context_tokens_is_3072(self) -> None:
        """Default PromptConfig must have max_context_tokens=3072."""
        cfg = PromptConfig()
        assert cfg.max_context_tokens == 3072

    def test_custom_max_context_tokens_accepted(self) -> None:
        """Any value within [256, 16384] must be accepted."""
        cfg = PromptConfig(max_context_tokens=1024)
        assert cfg.max_context_tokens == 1024

    def test_max_context_tokens_boundary_min(self) -> None:
        """Minimum value 256 must be valid."""
        cfg = PromptConfig(max_context_tokens=256)
        assert cfg.max_context_tokens == 256

    def test_max_context_tokens_boundary_max(self) -> None:
        """Maximum value 16384 must be valid."""
        cfg = PromptConfig(max_context_tokens=16384)
        assert cfg.max_context_tokens == 16384

    def test_max_context_tokens_below_min_raises(self) -> None:
        """Value below 256 must raise ValidationError."""
        with pytest.raises(ValidationError):
            PromptConfig(max_context_tokens=255)

    def test_max_context_tokens_above_max_raises(self) -> None:
        """Value above 16384 must raise ValidationError."""
        with pytest.raises(ValidationError):
            PromptConfig(max_context_tokens=16385)

    def test_max_context_tokens_survives_round_trip(self) -> None:
        """max_context_tokens must survive model_dump → model_validate round-trip."""
        original = PromptConfig(max_context_tokens=4096)
        restored = PromptConfig.model_validate(original.model_dump())
        assert restored.max_context_tokens == 4096
