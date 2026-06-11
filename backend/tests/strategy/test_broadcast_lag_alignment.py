"""Regression: EOD-published broadcast features (VIX, F&O, bulk deals,
delivery %) must be merged into the DAILY training matrix as-of the PRIOR
session, and the news window must extend to the entry bar.

The heartbeat runs mid-session, before the day's EOD publications land
(bulk deals + delivery publish after close, ingest-vix 16:00, ingest-fno
18:30) — so the freshest value live inference can see is the prior
session's. Training merged same-day EOD values until schema v4, teaching
a lag-0 relationship serving could only feed at lag-1.
"""

from datetime import datetime, timedelta

import pytest

from yolovest.skills.model_retrain import ModelRetrainSkill

_N_BARS = 230  # window_size 200 + lookahead 10 + slack
_BASE = datetime(2025, 1, 1)


def _bars(n: int = _N_BARS, symbol: str = "RELIANCE") -> list[dict]:
    out = []
    close = 100.0
    for i in range(n):
        close *= (1 + (0.01 if i % 3 == 0 else -0.008 if i % 3 == 1 else 0.002))
        out.append({
            "symbol": symbol,
            "timestamp": (_BASE + timedelta(days=i)).isoformat(),
            "open": close * 0.998,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1_000_000 + i * 100,
        })
    return out


def _date_str(i: int) -> str:
    return (_BASE + timedelta(days=i)).strftime("%Y-%m-%d")


@pytest.fixture
def skill(app_context):
    return ModelRetrainSkill(app_context)


def _column(feature_names: list[str], name: str) -> int:
    assert name in feature_names, f"{name} missing from feature_names"
    return feature_names.index(name)


class TestPriorSessionAsOf:
    def test_vix_is_prior_session_value(self, skill):
        bars = _bars()
        # Distinct VIX value per calendar day: value == day index + 10.
        vix_timeline = [(_date_str(i), float(i + 10)) for i in range(_N_BARS)]

        X, _y, names, _w, meta = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=10, vix_timeline=vix_timeline,
        )
        col = _column(names, "vix_level")
        # Row r corresponds to bar index i = 200 + r (single symbol,
        # chronological). The merged VIX must be bars[i-1]'s value —
        # i.e. (i-1) + 10 — NOT the same-day (i + 10).
        for r in (0, 5, len(X) - 1):
            i = 200 + r
            assert X[r][col] == float((i - 1) + 10), (
                f"row {r}: vix_level={X[r][col]} is not the prior session's "
                f"value {(i - 1) + 10}"
            )

    def test_fno_is_prior_session_value(self, skill):
        # The fno feature group defaults OFF (forward-only data source);
        # enable it so the merge lands in the matrix for this test.
        skill.ctx.config.strategy.feature_groups.fno = True
        bars = _bars()
        fno_lookup = {
            "RELIANCE": [
                (_date_str(i), {
                    "pcr_oi": float(i + 1),
                    "pcr_volume": 1.0,
                    "futures_oi": 1000.0,
                    "futures_volume": 100.0,
                    "futures_close": 100.0,
                })
                for i in range(_N_BARS)
            ],
        }
        X, _y, names, _w, _meta = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=10, fno_lookup=fno_lookup,
        )
        col = _column(names, "pcr_oi")
        for r in (0, len(X) - 1):
            i = 200 + r
            assert X[r][col] == float((i - 1) + 1)

    def test_bulk_deals_window_ends_at_prior_session(self, skill):
        bars = _bars()
        # One BUY deal exactly on the first sample's own date (i=200).
        # Under prior-session as-of it must NOT count at row 0, but it
        # must count at row 1 (whose prior session IS day 200).
        deal_day = _date_str(200)
        bulk_deal_lookup = {("RELIANCE", deal_day): {"buy": 1, "sell": 0}}

        X, _y, names, _w, _meta = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=10,
            bulk_deal_lookup=bulk_deal_lookup,
        )
        col = _column(names, "bulk_deal_buy_5d")
        assert X[0][col] == 0.0, "same-day bulk deal leaked into the sample"
        assert X[1][col] == 1.0, "prior-session bulk deal missing"

    def test_delivery_pct_excludes_same_day(self, skill):
        bars = _bars()
        # Stamp a distinctive delivery % only on the first sample's own
        # day (i=200): excluded at row 0, included at rows 1..5.
        bars[200]["delivery_pct"] = 80.0
        X, _y, names, _w, _meta = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=10,
        )
        col = _column(names, "delivery_pct_avg_5d")
        assert X[0][col] == 0.0, "same-day delivery % leaked into the sample"
        assert X[1][col] == 80.0

    def test_news_window_extends_to_entry_bar(self, skill):
        bars = _bars()
        # Headline published mid-day on the first sample's decision day
        # (i=200). The old midnight-of-bar cutoff excluded it; the entry-
        # bar cutoff (midnight opening day 201) must include it in the
        # trailing-24h window.
        published = (_BASE + timedelta(days=200, hours=10)).isoformat()
        news_lookup = {"RELIANCE": [("Reliance wins arbitration", published)]}

        X, _y, names, _w, _meta = skill._prepare_training_data(
            {"bars": bars}, lookahead_bars=10, news_lookup=news_lookup,
        )
        col = _column(names, "news_count_24h")
        assert X[0][col] == 1.0, (
            "decision-day headline missing from the entry-bar news window"
        )
