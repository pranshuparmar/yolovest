"""Direct tests of `ModelRetrainSkill._path_aware_label`.

Covers the geometry around the "both legs win on different bars"
disambiguation introduced to address the swing model's 87% HOLD label
inflation. See `skills/model_retrain.py::_path_aware_label`.
"""

from datetime import datetime, timedelta

from yolovest.models.schemas import OHLCVBar
from yolovest.skills.model_retrain import ModelRetrainSkill

_BASE = datetime(2026, 1, 1)


def _bar(idx: int, o: float, h: float, lo: float, c: float) -> OHLCVBar:
    return OHLCVBar(
        symbol="TEST",
        timestamp=_BASE + timedelta(days=idx),
        open=o, high=h, low=lo, close=c, volume=1_000_000,
    )


def _label(bars: list[OHLCVBar], **kwargs) -> int:
    return ModelRetrainSkill._path_aware_label(
        bars=bars,
        start_idx=0,
        lookahead=kwargs.pop("lookahead", 5),
        entry=kwargs.pop("entry", 100.0),
        target_pct=kwargs.pop("target_pct", 0.03),
        sl_pct=kwargs.pop("sl_pct", 0.015),
    )


class TestPathAwareLabel:
    def test_buy_wins_cleanly(self):
        """Up-only path: BUY target hits, SL never touched, SELL leg loses."""
        bars = [
            _bar(0, 100, 100, 100, 100),   # entry bar
            _bar(1, 100, 101, 99.5, 101),
            _bar(2, 101, 103.5, 100.5, 103),  # buy_target=103 hit on high
            _bar(3, 103, 104, 102, 103.5),
            _bar(4, 103, 104, 102.5, 104),
            _bar(5, 104, 105, 103, 104.5),
        ]
        assert _label(bars) == 2  # BUY

    def test_sell_wins_cleanly(self):
        """Down-only path: SELL target hits, SL never touched."""
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 100.5, 99, 99),
            _bar(2, 99, 99.5, 96.5, 97),   # sell_target=97 hit on low
            _bar(3, 97, 98, 96, 96.5),
            _bar(4, 96.5, 97, 95.5, 96),
            _bar(5, 96, 96.5, 95, 95.5),
        ]
        assert _label(bars) == 0  # SELL

    def test_buy_wins_first_then_sell_wins_later(self):
        """The case that was inflating HOLD labels. BUY wins on bar 2,
        SELL wins on bar 4. Old labeler returned HOLD ("both won");
        new labeler returns BUY (first winner).
        """
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 101, 99.5, 101),
            _bar(2, 101, 103.5, 100.5, 103),  # buy_target=103 hit FIRST
            _bar(3, 103, 103.5, 100, 100.5),
            _bar(4, 100.5, 101, 96.5, 97),    # sell_target=97 hit LATER
            _bar(5, 97, 98, 96, 96.5),
        ]
        assert _label(bars) == 2  # BUY (first winner)

    def test_sell_wins_first_then_buy_wins_later(self):
        """Mirror of above — SELL wins on bar 2, BUY on bar 4 → SELL."""
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 100.5, 99, 99),
            _bar(2, 99, 99.5, 96.5, 97),     # sell_target=97 hit FIRST
            _bar(3, 97, 100, 96.8, 100),
            _bar(4, 100, 103.5, 99.5, 103),  # buy_target=103 hit LATER
            _bar(5, 103, 104, 102, 103.5),
        ]
        assert _label(bars) == 0  # SELL (first winner)

    def test_both_wins_same_bar_falls_back_to_hold(self):
        """If both legs win on the same bar (intra-bar order unknown)
        the labeler stays conservative and returns HOLD.
        """
        bars = [
            _bar(0, 100, 100, 100, 100),
            # Single-bar massive range: high 103.5 (BUY target), low 96.5 (SELL target).
            _bar(1, 100, 103.5, 96.5, 100),
        ]
        assert _label(bars, lookahead=1) == 1  # HOLD

    def test_buy_loses_sl_first(self):
        """BUY SL hits first → BUY leg = loss. SELL never targets → SELL leg = None.
        Result: HOLD (neither won).
        """
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 100.5, 98, 98.5),   # BUY SL=98.5 hit on low; SELL nothing
            _bar(2, 98.5, 99, 98, 98.2),
            _bar(3, 98, 99, 98, 98.5),
            _bar(4, 98, 99, 98, 98.7),
            _bar(5, 98.7, 99, 98.5, 99),
        ]
        assert _label(bars) == 1  # HOLD

    def test_no_outcomes_in_window(self):
        """Tight sideways path that touches no barrier → HOLD."""
        bars = [
            _bar(0, 100, 100, 100, 100),
            _bar(1, 100, 100.2, 99.8, 100.1),
            _bar(2, 100.1, 100.3, 99.7, 100),
            _bar(3, 100, 100.2, 99.8, 99.9),
            _bar(4, 99.9, 100.1, 99.7, 100),
            _bar(5, 100, 100.2, 99.8, 100),
        ]
        assert _label(bars) == 1  # HOLD


class TestIntradayPathAwareLabel:
    """`intraday_path_aware_label` mirrors the daily geometry but with a
    hard same-session close-out — a barrier touched only in a later
    session must not count toward the label."""

    @staticmethod
    def _ibar(day: int, hh: int, mm: int, h: float, lo: float) -> OHLCVBar:
        return OHLCVBar(
            symbol="TEST",
            timestamp=datetime(2026, 5, 20 + day, hh, mm),
            open=100.0, high=h, low=lo, close=100.0, volume=1_000_000,
        )

    def _ilabel(self, bars: list[OHLCVBar], lookahead: int = 6) -> int:
        from yolovest.skills.model_retrain import intraday_path_aware_label
        return intraday_path_aware_label(
            bars=bars, start_idx=0, lookahead=lookahead,
            entry=100.0, target_pct=0.006, sl_pct=0.003,
        )

    def test_buy_wins_same_session(self):
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 9, 20, 100.7, 99.9)]
        assert self._ilabel(bars) == 2

    def test_sell_wins_same_session(self):
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 9, 20, 100.1, 99.3)]
        assert self._ilabel(bars) == 0

    def test_sl_only_is_hold(self):
        # BUY SL touched, no clean win → HOLD.
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 9, 20, 100.2, 99.6)]
        assert self._ilabel(bars) == 1

    def test_timeout_within_session_is_hold(self):
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 9, 20, 100.2, 99.8),
                self._ibar(0, 9, 25, 100.2, 99.8)]
        assert self._ilabel(bars) == 1

    def test_cross_session_barrier_does_not_count(self):
        # Target is only reached on the NEXT day — same-day close-out must
        # stop the walk at the boundary and return HOLD, not BUY.
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 15, 25, 100.2, 99.8),
                self._ibar(1, 9, 15, 101.0, 99.0)]
        assert self._ilabel(bars) == 1

    def test_ambiguous_same_bar_is_hold(self):
        bars = [self._ibar(0, 9, 15, 100, 100),
                self._ibar(0, 9, 20, 100.7, 99.3)]
        assert self._ilabel(bars) == 1
