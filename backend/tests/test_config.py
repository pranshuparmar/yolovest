"""Tests for config validation in config.py.

Tests valid config loading, validation errors for invalid data,
environment variable expansion, and missing required fields.
"""

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from yolovest.config import (
    AppConfig,
    MarketHoursConfig,
    RiskConfig,
    ScanningConfig,
    ScanningWeights,
    _expand_env_vars,
)

# Alias for tests that reference the old recursive function name
_expand_env_recursive = _expand_env_vars


class TestValidConfigLoads:
    def test_default_config_is_valid(self):
        config = AppConfig()
        assert config.mode == "paper"
        assert config.capital.initial_amount == 100_000
        assert config.risk.max_risk_per_trade_pct == 0.02

    def test_config_from_fixture_is_valid(self, sample_config):
        assert sample_config.mode == "paper"
        assert sample_config.capital.initial_amount == 100000
        assert sample_config.scanning.weights.technical == 0.35

    def test_config_live_mode(self):
        config = AppConfig(mode="live")
        assert config.mode == "live"


class TestScanningWeightsValidation:
    def test_weights_summing_to_one_passes(self):
        w = ScanningWeights(
            technical=0.30, volume_momentum=0.25,
            news_sentiment=0.20, fundamental=0.15, volatility=0.10,
        )
        assert w.technical == 0.30

    def test_weights_not_summing_to_one_raises(self):
        with pytest.raises(ValidationError, match=r"[Ww]eights.*sum to 1.0"):
            ScanningWeights(
                technical=0.50, volume_momentum=0.30,
                news_sentiment=0.20, fundamental=0.20,
            )

    def test_weights_below_one_raises(self):
        with pytest.raises(ValidationError, match=r"[Ww]eights.*sum to 1.0"):
            ScanningWeights(
                technical=0.10, volume_momentum=0.10,
                news_sentiment=0.10, fundamental=0.10,
            )


class TestRiskPctValidation:
    def test_risk_pct_valid_values(self):
        risk = RiskConfig(
            max_risk_per_trade_pct=0.01,
            max_portfolio_exposure_pct=0.50,
            max_single_stock_pct=0.10,
            daily_loss_limit_pct=0.02,
            weekly_loss_limit_pct=0.04,
        )
        assert risk.max_risk_per_trade_pct == 0.01

    def test_risk_pct_zero_raises(self):
        with pytest.raises(ValidationError, match="max_risk_per_trade_pct"):
            RiskConfig(max_risk_per_trade_pct=0.0)

    def test_risk_pct_one_raises(self):
        with pytest.raises(ValidationError, match="max_risk_per_trade_pct"):
            RiskConfig(max_risk_per_trade_pct=1.0)

    def test_risk_pct_negative_raises(self):
        with pytest.raises(ValidationError, match="max_risk_per_trade_pct"):
            RiskConfig(max_risk_per_trade_pct=-0.01)

    def test_risk_pct_above_one_raises(self):
        with pytest.raises(ValidationError, match="max_portfolio_exposure_pct"):
            RiskConfig(max_portfolio_exposure_pct=1.5)

    def test_daily_loss_limit_zero_raises(self):
        with pytest.raises(ValidationError, match="daily_loss_limit_pct"):
            RiskConfig(daily_loss_limit_pct=0.0)

    def test_weekly_loss_limit_one_raises(self):
        with pytest.raises(ValidationError, match="weekly_loss_limit_pct"):
            RiskConfig(weekly_loss_limit_pct=1.0)


class TestMarketHoursValidation:
    def test_valid_time_ordering(self):
        mh = MarketHoursConfig(
            open="09:15",
            close="15:30",
            order_start="09:15",
            order_end="15:15",
        )
        assert mh.order_start == "09:15"

    def test_order_start_before_market_open_raises(self):
        with pytest.raises(ValidationError, match=r"order_start.*before.*open"):
            MarketHoursConfig(
                open="09:15",
                close="15:30",
                order_start="09:00",
                order_end="15:15",
            )

    def test_order_end_after_market_close_raises(self):
        with pytest.raises(ValidationError, match=r"order_end.*after.*close"):
            MarketHoursConfig(
                open="09:15",
                close="15:30",
                order_start="09:15",
                order_end="16:00",
            )

    def test_square_off_after_close_raises(self):
        with pytest.raises(ValidationError, match=r"square_off.*after.*close"):
            MarketHoursConfig(
                open="09:15",
                close="15:30",
                order_start="09:15",
                order_end="15:15",
                square_off="16:00",
            )


class TestModeValidation:
    def test_mode_paper_valid(self):
        config = AppConfig(mode="paper")
        assert config.mode == "paper"

    def test_mode_live_valid(self):
        config = AppConfig(mode="live")
        assert config.mode == "live"

    def test_mode_invalid_raises(self):
        with pytest.raises(ValidationError, match="mode"):
            AppConfig(mode="simulation")

    def test_mode_empty_raises(self):
        with pytest.raises(ValidationError, match="mode"):
            AppConfig(mode="")


class TestEnvironmentVariableExpansion:
    def test_expand_env_var_in_string(self):
        with patch.dict(os.environ, {"TEST_API_KEY": "secret123"}):
            result = _expand_env_vars("${TEST_API_KEY}")
            assert result == "secret123"

    def test_expand_env_recursive_dict(self):
        with patch.dict(os.environ, {"MY_KEY": "val"}):
            result = _expand_env_recursive({"key": "${MY_KEY}"})
            assert result == {"key": "val"}

    def test_expand_env_recursive_list(self):
        with patch.dict(os.environ, {"MY_VAL": "x"}):
            result = _expand_env_recursive(["${MY_VAL}", "literal"])
            assert result == ["x", "literal"]

    def test_expand_missing_env_var_leaves_unexpanded(self):
        """The existing impl leaves ${VAR} unexpanded if env var is not set."""
        os.environ.pop("NONEXISTENT_VAR_12345", None)
        result = _expand_env_vars("${NONEXISTENT_VAR_12345}")
        assert result == "${NONEXISTENT_VAR_12345}"

    def test_expand_no_env_var_passthrough(self):
        result = _expand_env_vars("plain_string")
        assert result == "plain_string"

    def test_expand_recursive_non_string_passthrough(self):
        assert _expand_env_recursive(42) == 42
        assert _expand_env_recursive(3.14) == 3.14
        assert _expand_env_recursive(True) is True


class TestMissingRequiredFields:
    def test_scanning_default_weights_sum_to_one(self):
        config = ScanningConfig()
        w = config.weights
        total = w.technical + w.volume_momentum + w.news_sentiment + w.fundamental + w.volatility
        assert abs(total - 1.0) < 1e-6

    def test_risk_uses_defaults(self):
        risk = RiskConfig()
        assert risk.max_risk_per_trade_pct == 0.02
        assert risk.max_open_positions == 10


class TestConfigFieldKinds:
    """field kinds come from Pydantic ANNOTATIONS, not values — JSON
    erases int/float (1.0 -> 1), which made the Settings UI reject
    decimals on whole-valued float fields like time_decay_last_weight."""

    def test_float_fields_classified_float_even_when_default_is_whole(self):
        from yolovest.config import config_field_kinds

        kinds = config_field_kinds()
        for key in (
            "strategy.time_decay_last_weight",      # default 1.0
            "strategy.feedback.sample_weight_boost",  # default 2.0
            "strategy.holding_periods.long.target",   # default 5.0
            "strategy.relative_label_quantile",
        ):
            assert kinds.get(key) == "float", key

    def test_int_fields_classified_int(self):
        from yolovest.config import config_field_kinds

        kinds = config_field_kinds()
        for key in (
            "risk.max_open_positions",
            "retraining.max_training_days",
            "strategy.swing_horizon_cap_days",
        ):
            assert kinds.get(key) == "int", key

    def test_optional_numbers_unwrap(self):
        from yolovest.config import config_field_kinds

        kinds = config_field_kinds()
        assert kinds.get("risk.max_mis_trades_per_day") == "int"
        assert kinds.get("risk.buy_threshold_override") == "float"

    def test_bools_and_strings_excluded(self):
        from yolovest.config import config_field_kinds

        kinds = config_field_kinds()
        assert "llm.enabled" not in kinds
        assert "strategy.mode" not in kinds
