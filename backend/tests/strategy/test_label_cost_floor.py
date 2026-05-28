"""Cost-aware label floor: a triple-barrier target must clear the
round-trip transaction cost + slippage, else a 'win' is a net loss and
must not be labelled BUY/SELL. Verifies both the cost helper and that the
floor is threaded into the swing label geometry + stored bars_meta.
"""

from datetime import datetime, timedelta

import pytest

from yolovest.config import TransactionCostConfig
from yolovest.costs import round_trip_cost_floor_pct
from yolovest.skills.model_retrain import ModelRetrainSkill


def _bars(n: int, symbol: str = "RELIANCE") -> list[dict]:
    base = datetime(2025, 1, 1)
    out = []
    close = 100.0
    for i in range(n):
        close *= (1 + (0.01 if i % 3 == 0 else -0.008 if i % 3 == 1 else 0.002))
        out.append({
            "symbol": symbol,
            "timestamp": (base + timedelta(days=i)).isoformat(),
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1_000_000 + i * 100,
        })
    return out


@pytest.fixture
def skill(app_context):
    return ModelRetrainSkill(app_context)


class TestRoundTripCostFloor:
    def test_default_rates_mis_vs_cnc(self):
        # MIS: 2*0.0003 + 0.00025 + 2*0.0001 + 2*0.0005 = 0.00205
        assert round_trip_cost_floor_pct("MIS") == pytest.approx(0.00205)
        # CNC: 2*0.0003 + 0.001 + 2*0.0001 + 2*0.0005 = 0.0028
        assert round_trip_cost_floor_pct("CNC") == pytest.approx(0.0028)
        # CNC carries the heavier two-sided STT, so it floors higher.
        assert round_trip_cost_floor_pct("CNC") > round_trip_cost_floor_pct("MIS")

    def test_respects_cost_config_overrides(self):
        cfg = TransactionCostConfig(
            brokerage_per_leg_pct=0.0,
            stt_intraday_pct=0.0,
            stt_delivery_pct=0.0,
            other_charges_pct=0.0,
        )
        # Only slippage remains: 2 * 0.0005.
        assert round_trip_cost_floor_pct("MIS", cfg) == pytest.approx(0.001)
        # Slippage override flows through too.
        assert round_trip_cost_floor_pct("MIS", cfg, slippage_pct=0.0) == 0.0


class TestLabelCostFloorWiring:
    def test_floor_lifts_stored_target_pct(self, skill):
        td = {"bars": _bars(300)}
        # No floor → bars_meta target is the raw ATR-derived value.
        _, _, _, _, meta0 = skill._prepare_training_data(
            td, lookahead_bars=10, target_atr_mult=1.5, sl_atr_mult=0.75,
            cost_floor_pct=0.0,
        )
        # Huge floor (10%) → every stored target is lifted to the floor,
        # since these ~1% bars never produce a 10% ATR target.
        floor = 0.10
        _, _, _, _, metaf = skill._prepare_training_data(
            td, lookahead_bars=10, target_atr_mult=1.5, sl_atr_mult=0.75,
            cost_floor_pct=floor,
        )
        assert meta0 and metaf
        assert all(m["target_pct"] < floor for m in meta0)
        assert all(m["target_pct"] == pytest.approx(floor) for m in metaf)

    def test_unreachable_floor_collapses_non_hold_labels(self, skill):
        td = {"bars": _bars(300)}
        _, y0, _, _, _ = skill._prepare_training_data(
            td, lookahead_bars=10, target_atr_mult=1.5, sl_atr_mult=0.75,
            cost_floor_pct=0.0,
        )
        _, yf, _, _, _ = skill._prepare_training_data(
            td, lookahead_bars=10, target_atr_mult=1.5, sl_atr_mult=0.75,
            cost_floor_pct=0.10,
        )
        # A 10% target is unreachable by these bars → no BUY/SELL wins.
        assert sum(1 for v in yf if v != 1) == 0
        # And the floor only removes signals (never invents them).
        assert sum(1 for v in yf if v != 1) <= sum(1 for v in y0 if v != 1)
