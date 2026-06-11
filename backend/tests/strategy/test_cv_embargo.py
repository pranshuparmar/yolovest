"""CV/holdout embargo: the purge boundary must drop an extra buffer of
train rows beyond label overlap to absorb serial-correlation leakage.
"""

from datetime import date, timedelta

from yolovest.config import RetrainingConfig
from yolovest.strategy.ml_signal import _purge_boundary


def _daily_meta(n: int) -> list[dict]:
    base = date(2020, 1, 1)
    return [{"entry_date": (base + timedelta(days=i)).isoformat()} for i in range(n)]


def test_embargo_default_is_one_percent():
    assert RetrainingConfig().cv_embargo_frac == 0.01


def test_embargo_widens_purge_gap():
    meta = _daily_meta(400)
    cut = 300  # holdout starts at index 300 (= day 300)
    # Label-overlap purge only: int(10*7/5)+2 = 16 calendar days back.
    base = _purge_boundary(meta, cut, lookahead_bars=10, min_keep=10, embargo_days=0)
    assert base == cut - 16
    # Embargo adds 30 more calendar days of buffer.
    emb = _purge_boundary(meta, cut, lookahead_bars=10, min_keep=10, embargo_days=30)
    assert emb == cut - (16 + 30)
    assert emb < base


def test_embargo_respects_min_keep_floor():
    meta = _daily_meta(400)
    # An absurd embargo can't starve the tuning fit below min_keep.
    kept = _purge_boundary(meta, 300, lookahead_bars=10, min_keep=50, embargo_days=10_000)
    assert kept == 50


def test_embargo_alone_applies_without_lookahead():
    # Even with no label lookahead, a positive embargo still buffers.
    meta = _daily_meta(400)
    kept = _purge_boundary(meta, 300, lookahead_bars=0, min_keep=10, embargo_days=20)
    # gap = int(0*7/5)+2+20 = 22 days
    assert kept == 300 - 22
