"""Tests for `intraday_triple_barrier_label` — the 1-minute-path-resolved
triple-barrier label for the intraday model.

The point of this label vs. a 5-min-only path walk is
that it resolves the intra-5-min-bar ambiguity (did the high/target or the
low/SL print first?) using real 1-min data instead of collapsing to HOLD.
"""

from datetime import datetime, timedelta

from yolovest.models.schemas import OHLCVBar
from yolovest.skills.model_retrain import intraday_triple_barrier_label

# 09:20 IST entry — a fresh session, away from the open auction.
_ENTRY_TIME = datetime(2026, 1, 5, 9, 20)


def _m(minute_offset: int, hi: float, lo: float) -> OHLCVBar:
    """A 1-min bar `minute_offset` minutes after the entry time."""
    ts = _ENTRY_TIME + timedelta(minutes=minute_offset)
    mid = (hi + lo) / 2
    return OHLCVBar(
        symbol="TEST", timestamp=ts, open=mid, high=hi, low=lo,
        close=mid, volume=10_000,
    )


def _label(minute_bars, **kw) -> int:
    return intraday_triple_barrier_label(
        entry=kw.pop("entry", 100.0),
        entry_time=kw.pop("entry_time", _ENTRY_TIME),
        horizon_minutes=kw.pop("horizon_minutes", 60),
        target_pct=kw.pop("target_pct", 0.01),   # +/-1%
        sl_pct=kw.pop("sl_pct", 0.005),           # +/-0.5%
        minute_bars=minute_bars,
        start_idx=kw.pop("start_idx", 0),
    )


class TestCleanOutcomes:
    def test_buy_wins_cleanly(self):
        # price drifts up, tags +1% target (101) before the -0.5% stop (99.5)
        bars = [_m(0, 100.3, 99.9), _m(1, 100.8, 100.1), _m(2, 101.2, 100.6)]
        assert _label(bars) == 2

    def test_sell_wins_cleanly(self):
        # price drifts down, tags -1% target (99) before the +0.5% stop (100.5)
        bars = [_m(0, 100.1, 99.7), _m(1, 99.9, 99.3), _m(2, 99.5, 98.9)]
        assert _label(bars) == 0

    def test_no_barrier_hit_is_hold(self):
        bars = [_m(0, 100.2, 99.8), _m(1, 100.3, 99.7), _m(2, 100.1, 99.9)]
        assert _label(bars) == 1

    def test_buy_loses_when_sl_first(self):
        # dips to the -0.5% stop (99.5) before any +1% target
        bars = [_m(0, 100.1, 99.4), _m(1, 100.2, 99.8)]
        # BUY stopped; SELL: did it tag -1% (99) before +0.5% (100.5)? No → HOLD
        assert _label(bars) == 1


class TestIntraBarAmbiguityResolved:
    """The headline improvement: a move that a 5-min bar would see as
    'both target and SL inside one bar' (→ HOLD) is decided by the 1-min
    order of touches."""

    def test_target_before_stop_within_the_window(self):
        # First minute pops to the +1% target (101); a later minute dips to
        # the -0.5% stop. On a single 5-min bar both would appear together;
        # at 1-min the target clearly comes first → BUY.
        bars = [_m(0, 101.1, 100.0), _m(1, 100.6, 99.3)]
        assert _label(bars) == 2

    def test_stop_before_target_within_the_window(self):
        # Reverse order: stop first, then the target later → BUY loses; and
        # SELL never tags its -1% target → HOLD.
        bars = [_m(0, 100.2, 99.4), _m(1, 101.2, 100.0)]
        assert _label(bars) == 1

    def test_single_bar_straddle_breaks_to_sl(self):
        # One 1-min bar that straddles BOTH the +1% target and -0.5% stop:
        # conservative tie → SL (BUY loses). SELL also straddles its pair
        # → loss. Neither wins → HOLD.
        bars = [_m(0, 101.5, 99.0)]
        assert _label(bars) == 1


class TestSessionAndHorizonGuards:
    def test_no_overnight_carry(self):
        # Target only prints the NEXT calendar day — must not count.
        next_day = [
            OHLCVBar(symbol="TEST",
                     timestamp=datetime(2026, 1, 6, 9, 20),
                     open=100.0, high=101.5, low=100.0, close=101.5,
                     volume=10_000),
        ]
        bars = [_m(0, 100.2, 99.8)] + next_day
        assert _label(bars) == 1

    def test_horizon_cutoff_excludes_late_touch(self):
        # Target prints at +65 min, past the 60-min horizon → not counted.
        bars = [_m(0, 100.2, 99.8), _m(65, 101.5, 100.0)]
        assert _label(bars, horizon_minutes=60) == 1

    def test_touch_exactly_inside_horizon_counts(self):
        bars = [_m(0, 100.2, 99.8), _m(59, 101.1, 100.0)]
        assert _label(bars, horizon_minutes=60) == 2

    def test_start_idx_past_end_is_hold(self):
        bars = [_m(0, 101.5, 99.0)]
        assert _label(bars, start_idx=5) == 1

    def test_start_idx_skips_pre_entry_bars(self):
        # Bars before the fill must be ignored via start_idx.
        pre = OHLCVBar(symbol="TEST",
                       timestamp=_ENTRY_TIME - timedelta(minutes=1),
                       open=100.0, high=101.5, low=100.0, close=101.5,
                       volume=10_000)  # a spike BEFORE entry
        bars = [pre, _m(0, 100.2, 99.8), _m(1, 100.1, 99.9)]
        # With start_idx=1 the pre-entry spike is skipped → no target → HOLD
        assert _label(bars, start_idx=1) == 1


class TestEarlyResolution:
    def test_winner_resolved_on_first_touch(self):
        # BUY tags +1% at minute 0 (and the same up-move stops the SELL leg
        # at +0.5%), so the label is decided immediately as BUY — later bars
        # don't matter. With target_pct > sl_pct a genuine both-sides-win is
        # geometrically impossible: the winning move always trips the other
        # side's stop first.
        bars = [_m(0, 101.1, 100.0), _m(1, 100.5, 99.6), _m(2, 100.2, 98.9)]
        assert _label(bars) == 2
