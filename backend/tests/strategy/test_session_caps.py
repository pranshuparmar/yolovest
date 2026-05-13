"""Tests for apply_session_caps — circuit-limit enforcement only.

Day's high/low are intentionally NOT forward caps (they're just where the
stock has traded so far; legitimate breakouts push past them). Only the
exchange's upper/lower circuit limits are real forward boundaries that
orders cannot cross.
"""

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
    def test_target_above_day_high_is_allowed(self):
        """A target above today's high is a legitimate breakout target
        and must pass through unmodified."""
        target, sl, adj = apply_session_caps(
            "BUY", target=108.0, stop_loss=95.0, quote=quote(),
        )
        assert target == 108.0
        assert sl == 95.0
        assert adj == []

    def test_target_at_upper_circuit_capped(self):
        target, _, adj = apply_session_caps(
            "BUY", target=115.0, stop_loss=95.0, quote=quote(),
        )
        assert target == 110.0 * 0.99
        assert any("upper circuit" in a for a in adj)

    def test_sl_below_lower_circuit_floored(self):
        _, sl, adj = apply_session_caps(
            "BUY", target=99.0, stop_loss=75.0, quote=quote(),
        )
        assert sl == 80.0 * 1.01
        assert any("lower circuit" in a for a in adj)


class TestSellCaps:
    def test_target_below_day_low_is_allowed(self):
        """Mirror of the BUY breakout case — a SELL target below today's
        low is a legitimate breakdown target."""
        target, sl, adj = apply_session_caps(
            "SELL", target=85.0, stop_loss=95.0, quote=quote(),
        )
        assert target == 85.0
        assert sl == 95.0
        assert adj == []

    def test_target_at_lower_circuit_capped(self):
        target, _, adj = apply_session_caps(
            "SELL", target=70.0, stop_loss=99.0, quote=quote(),
        )
        assert target == 80.0 * 1.01
        assert any("lower circuit" in a for a in adj)

    def test_sl_above_upper_circuit_capped(self):
        _, sl, adj = apply_session_caps(
            "SELL", target=92.0, stop_loss=115.0, quote=quote(),
        )
        assert sl == 110.0 * 0.99
        assert any("upper circuit" in a for a in adj)


class TestMissingQuoteFields:
    def test_empty_quote_is_passthrough(self):
        target, sl, adj = apply_session_caps(
            "BUY", target=108.0, stop_loss=95.0, quote={},
        )
        assert target == 108.0
        assert sl == 95.0
        assert adj == []

    def test_only_circuits_apply_when_present(self):
        target, sl, adj = apply_session_caps(
            "BUY", target=200.0, stop_loss=95.0,
            quote={"upper_circuit": 110.0},
        )
        assert target == 110.0 * 0.99
        assert sl == 95.0


class TestHDFCLIFEScenario:
    """The originally-flagged scenario: target 611.90 with day_high 608.50
    is a LEGITIMATE breakout target. Upper circuit at 661.95 is the real
    ceiling, and the target is well below it — should pass through."""

    def test_breakout_target_allowed(self):
        target, sl, adj = apply_session_caps(
            "BUY",
            target=611.90,
            stop_loss=596.40,
            quote=quote(high=608.50, low=598.85,
                        upper_circuit=661.95, lower_circuit=541.65),
        )
        assert target == 611.90
        assert sl == 596.40
        assert adj == []
