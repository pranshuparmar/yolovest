"""Tests for apply_session_caps — reality-check intraday targets/SLs."""

from yolovest.strategy.holding_period import apply_session_caps


def quote(**overrides):
    base = {
        "high": 100.0,
        "low": 90.0,
        "upper_circuit": 110.0,
        "lower_circuit": 80.0,
    }
    base.update(overrides)
    return base


class TestBuyCaps:
    def test_target_above_day_high_capped_at_day_high_plus_buffer(self):
        # Day high 100, ATR 4. Target 108 → cap at 100 + 0.15*4 = 100.6
        target, sl, adj = apply_session_caps(
            "BUY", target=108.0, stop_loss=95.0, atr=4.0, quote=quote(),
            atr_buffer=0.15,
        )
        assert target == 100.6
        assert sl == 95.0
        assert len(adj) == 1
        assert "day_high" in adj[0]

    def test_target_under_day_high_unchanged(self):
        target, sl, adj = apply_session_caps(
            "BUY", target=99.0, stop_loss=95.0, atr=4.0, quote=quote(),
        )
        assert target == 99.0
        assert sl == 95.0
        assert adj == []

    def test_target_above_upper_circuit_hard_cap(self):
        # Cap at upper_circuit * 0.99 when target meets or exceeds circuit.
        target, _, adj = apply_session_caps(
            "BUY", target=115.0, stop_loss=95.0, atr=4.0,
            quote=quote(high=130.0),  # high allows target by buffer math
            atr_buffer=10.0,            # disables the day-high cap
        )
        assert target == 110.0 * 0.99
        assert any("upper circuit" in a for a in adj)

    def test_sl_below_lower_circuit_floored(self):
        _, sl, adj = apply_session_caps(
            "BUY", target=99.0, stop_loss=75.0, atr=4.0,
            quote=quote(lower_circuit=80.0),
        )
        assert sl == 80.0 * 1.01
        assert any("lower circuit" in a for a in adj)


class TestSellCaps:
    def test_target_below_day_low_capped(self):
        # Day low 90, ATR 4, buffer 0.15 → cap at 90 - 0.6 = 89.4
        target, _, adj = apply_session_caps(
            "SELL", target=85.0, stop_loss=95.0, atr=4.0, quote=quote(),
        )
        assert target == 89.4
        assert any("day_low" in a for a in adj)

    def test_target_above_day_low_unchanged(self):
        target, _, adj = apply_session_caps(
            "SELL", target=91.0, stop_loss=99.0, atr=4.0, quote=quote(),
        )
        assert target == 91.0
        assert adj == []

    def test_target_below_lower_circuit_hard_cap(self):
        target, _, adj = apply_session_caps(
            "SELL", target=70.0, stop_loss=99.0, atr=4.0,
            quote=quote(low=50.0),  # low allows target by buffer
            atr_buffer=10.0,
        )
        assert target == 80.0 * 1.01
        assert any("lower circuit" in a for a in adj)


class TestMissingQuoteFields:
    def test_no_quote_data_is_passthrough(self):
        target, sl, adj = apply_session_caps(
            "BUY", target=108.0, stop_loss=95.0, atr=4.0, quote={},
        )
        assert target == 108.0
        assert sl == 95.0
        assert adj == []

    def test_partial_quote_only_applies_known_caps(self):
        # Only day_high present — circuit caps don't trigger
        target, sl, adj = apply_session_caps(
            "BUY", target=108.0, stop_loss=95.0, atr=4.0,
            quote={"high": 100.0},
        )
        assert target == 100.6  # day-high cap applies
        assert sl == 95.0


class TestHDFCLIFEScenario:
    """Reproduce the live scenario the cap was designed for."""

    def test_hdfclife_611_target_capped_under_day_high(self):
        # From a real session: day high 608.50, ATR 17.25, target 611.90.
        target, _, adj = apply_session_caps(
            "BUY",
            target=611.90,
            stop_loss=596.40,
            atr=17.25,
            quote=quote(high=608.50, low=598.85,
                        upper_circuit=661.95, lower_circuit=541.65),
            atr_buffer=0.15,
        )
        # cap = 608.50 + 0.15 * 17.25 = 611.0875
        assert abs(target - 611.0875) < 0.01
        assert any("day_high" in a for a in adj)
