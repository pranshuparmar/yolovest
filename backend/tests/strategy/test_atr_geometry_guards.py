"""ATR sanity-cap interpolation + jugaad trading-date normalization.

Guards added after corrupt OHLCV (a wrong-symbol bar giving ATR ~126% of
price) produced a +189% target / -94% SL signal.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from yolovest.config import HoldingPeriodConfig
from yolovest.data.jugaad import _ist_trading_date
from yolovest.strategy.holding_period import interpolate_atr_pct_cap

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


class TestInterpolateAtrPctCap:
    def test_anchors_match_config(self):
        hp = HoldingPeriodConfig()
        assert interpolate_atr_pct_cap(0, hp) == hp.intraday.max_atr_pct_for_target
        assert interpolate_atr_pct_cap(3, hp) == hp.short_swing.max_atr_pct_for_target
        assert interpolate_atr_pct_cap(5, hp) == hp.week.max_atr_pct_for_target
        assert interpolate_atr_pct_cap(22, hp) == hp.long.max_atr_pct_for_target

    def test_interpolates_between_anchors(self):
        hp = HoldingPeriodConfig()
        # day 4 sits between short_swing(3, .06) and week(5, .08) → .07
        assert interpolate_atr_pct_cap(4, hp) == 0.07

    def test_clamps_beyond_longest_anchor(self):
        hp = HoldingPeriodConfig()
        assert interpolate_atr_pct_cap(100, hp) == hp.long.max_atr_pct_for_target

    def test_swing_buckets_now_capped(self):
        # Regression: swing/week/long caps used to default to 0 (disabled),
        # leaving CNC signals unprotected against a blown-out ATR.
        hp = HoldingPeriodConfig()
        for d in (3, 5, 22):
            assert interpolate_atr_pct_cap(d, hp) > 0


class TestJugaadTradingDate:
    def test_tz_aware_utc_normalized_to_ist_day(self):
        # Mon 2026-05-18 00:00 IST carried as UTC is Sun 18:30 — the source
        # of the -1 day shift. Must resolve back to Monday.
        utc_repr = datetime(2026, 5, 18, 0, 0, tzinfo=IST).astimezone(UTC)
        assert utc_repr.weekday() == 6  # stored value reads as Sunday
        assert _ist_trading_date(utc_repr).isoformat() == "2026-05-18"

    def test_naive_date_passthrough(self):
        assert _ist_trading_date(datetime(2026, 5, 18, 0, 0)).isoformat() == "2026-05-18"

    def test_string_date_parsed(self):
        assert _ist_trading_date("2026-05-18").isoformat() == "2026-05-18"
