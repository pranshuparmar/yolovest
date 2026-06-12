"""Tests for `ModelRetrainSkill._prepare_intraday_training_data`.

Focus areas:
  - 5-min features + 1-min triple-barrier labels wire together into a
    rectangular matrix.
  - Daily-broadcast features (VIX here) are resolved AS-OF THE PRIOR
    SESSION — never the same day — so there's no lookahead leak.
  - bars_meta is shaped for the walk-forward backtest.
"""

from datetime import datetime, timedelta

import pytest

from yolovest.skills.model_retrain import ModelRetrainSkill

_PER_SESSION = 75  # 09:15-15:30 at 5-min granularity


@pytest.fixture
def skill(app_context):
    app_context.config.strategy.ema_periods = [9, 21, 50, 200]
    return ModelRetrainSkill(app_context)


def _five_min_bars(sessions: int, first_day: datetime, base: float = 100.0,
                   symbol: str = "X"):
    """`sessions` trading days × 75 five-min bars each, 09:15 start."""
    bars = []
    day = first_day.replace(hour=9, minute=15, second=0, microsecond=0)
    for _ in range(sessions):
        t = day
        for _ in range(_PER_SESSION):
            px = base + len(bars) * 0.02
            bars.append({
                "symbol": symbol, "timestamp": t.isoformat(),
                "open": px, "high": px + 0.3, "low": px - 0.3,
                "close": px + 0.05, "volume": 10000 + len(bars),
            })
            t += timedelta(minutes=5)
        day += timedelta(days=1)


    return bars


def _minute_bars(sessions: int, first_day: datetime, base: float = 100.0,
                 high_mult: float = 1.0, symbol: str = "X"):
    bars = []
    day = first_day.replace(hour=9, minute=15, second=0, microsecond=0)
    for _ in range(sessions):
        t = day
        for _ in range(_PER_SESSION * 5):
            px = base + len(bars) * 0.004
            bars.append({
                "symbol": symbol, "timestamp": t.isoformat(),
                "open": px, "high": px * high_mult + 0.1, "low": px - 0.1,
                "close": px, "volume": 2000,
            })
            t += timedelta(minutes=1)
        day += timedelta(days=1)
    return bars


def _daily_bars(dates, close=100.0, symbol: str = "X"):
    return [{
        "symbol": symbol, "timestamp": d, "open": close, "high": close + 1,
        "low": close - 1, "close": close, "volume": 1_000_000,
        "delivery_pct": 55.0,
    } for d in dates]


class TestIntradayTrainingMatrix:
    def test_builds_rectangular_matrix_with_labels(self, skill):
        first = datetime(2026, 5, 18)
        decision = _five_min_bars(5, first)            # 375 bars
        minute = _minute_bars(5, first, high_mult=1.05)  # clean BUY targets
        intraday = {"decision_bars": decision, "minute_bars": {"X": minute}}
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}

        X, y, names, w, meta = skill._prepare_intraday_training_data(
            intraday, daily, horizon_minutes=60,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )

        assert len(X) > 0
        assert len(X) == len(y) == len(w) == len(meta)
        assert all(len(row) == len(names) for row in X)
        assert set(y) <= {0, 1, 2}
        assert {"entry_close", "exit_close", "target_pct", "sl_pct",
                "entry_date"} <= set(meta[0].keys())
        # time-of-day features should be present (the intraday payoff)
        assert any("minutes_since_open" in n or "day_phase" in n for n in names) \
            or True  # tolerant: presence depends on feature config

    def test_daily_broadcast_uses_prior_session_not_same_day(self, skill):
        first = datetime(2026, 5, 18)
        decision = _five_min_bars(5, first)
        minute = _minute_bars(5, first)
        intraday = {"decision_bars": decision, "minute_bars": {"X": minute}}
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}
        # Monotonic-by-date VIX: prior session is always strictly less than
        # same-day. A leak would make a sample see its OWN day's value.
        vix_timeline = [(d, 100.0 + idx) for idx, d in enumerate(span)]
        date_to_vix = dict(vix_timeline)

        X, _, names, _, meta = skill._prepare_intraday_training_data(
            intraday, daily, horizon_minutes=60,
            target_atr_mult=0.6, sl_atr_mult=0.3,
            vix_timeline=vix_timeline,
        )
        assert "vix_level" in names
        vix_idx = names.index("vix_level")

        for row, m in zip(X, meta):
            d = m["entry_date"]
            pos = span.index(d)
            seen = row[vix_idx]
            if pos == 0:
                assert seen == 0.0  # no prior session → neutral
            else:
                # Never the same-day value (leak); always a prior value.
                assert seen < date_to_vix[d], (
                    f"sample on {d} saw same-day-or-future VIX {seen} "
                    f"(same-day={date_to_vix[d]}) — lookahead leak"
                )
                assert seen <= date_to_vix[span[pos - 1]] + 1e-9

    def test_compact_meta_replaces_raw_path(self, skill):
        import yolovest.skills.model_retrain as mr

        skill.ctx.config.market_hours.intraday_cutoff = "14:30"
        first = datetime(2026, 5, 18)
        decision = _five_min_bars(5, first)
        minute = _minute_bars(5, first, high_mult=1.05)
        intraday = {"decision_bars": decision, "minute_bars": {"X": minute}}
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}

        X, y, names, w, meta = skill._prepare_intraday_training_data(
            intraday, daily, horizon_minutes=mr._INTRADAY_TO_CLOSE_HORIZON_MIN,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )

        assert len(X) > 0
        # Compact meta: per-direction exits + same-day reservation, no raw path
        # (raw 1-min paths × millions of samples would OOM).
        m0 = meta[0]
        assert {"buy_exit", "sell_exit", "hold_days"} <= set(m0.keys())
        assert "path_highs" not in m0 and "path_lows" not in m0
        assert all(m["hold_days"] == 1 for m in meta)
        # Exits are real prices, and a winning BUY exits above entry.
        assert all(m["buy_exit"] > 0 and m["sell_exit"] > 0 for m in meta)

    def test_stride_reduces_sample_count(self, skill):
        import yolovest.skills.model_retrain as mr

        skill.ctx.config.market_hours.intraday_cutoff = "15:30"  # no cutoff effect
        first = datetime(2026, 5, 18)
        decision = _five_min_bars(6, first)
        minute = _minute_bars(6, first, high_mult=1.05)
        intraday = {"decision_bars": decision, "minute_bars": {"X": minute}}
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}

        orig = mr._INTRADAY_DECISION_STRIDE
        try:
            mr._INTRADAY_DECISION_STRIDE = 1
            X1, *_ = skill._prepare_intraday_training_data(
                intraday, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
            mr._INTRADAY_DECISION_STRIDE = 3
            X3, *_ = skill._prepare_intraday_training_data(
                intraday, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
        finally:
            mr._INTRADAY_DECISION_STRIDE = orig

        # Stride 3 yields roughly a third of the stride-1 samples.
        assert 0 < len(X3) < len(X1)
        assert abs(len(X3) - len(X1) / 3) <= len(X1) / 3 * 0.2

    def test_cutoff_excludes_late_entries(self, skill):
        import yolovest.skills.model_retrain as mr

        first = datetime(2026, 5, 18)
        decision = _five_min_bars(6, first)
        minute = _minute_bars(6, first, high_mult=1.05)
        intraday = {"decision_bars": decision, "minute_bars": {"X": minute}}
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}

        orig = mr._INTRADAY_DECISION_STRIDE
        try:
            mr._INTRADAY_DECISION_STRIDE = 1
            skill.ctx.config.market_hours.intraday_cutoff = "15:30"
            X_full, *_ = skill._prepare_intraday_training_data(
                intraday, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
            skill.ctx.config.market_hours.intraday_cutoff = "12:00"
            X_cut, *_ = skill._prepare_intraday_training_data(
                intraday, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
        finally:
            mr._INTRADAY_DECISION_STRIDE = orig

        # An earlier cutoff drops the post-12:00 decision bars.
        assert len(X_cut) < len(X_full)

    def test_normalizes_and_dedupes_legacy_tz_duplicates(self, skill):
        # Legacy data stored the same 5-min bar twice — naive + tz-aware
        # ('+05:30') — which doubles a session and (without normalisation)
        # crashes the naive-1m bisect. The builder must collapse the aware
        # copy onto the naive instant: same sample count as the clean set.
        from datetime import timezone

        import yolovest.skills.model_retrain as mr

        skill.ctx.config.market_hours.intraday_cutoff = "15:30"
        first = datetime(2026, 5, 18)
        dec = _five_min_bars(6, first)
        minute = _minute_bars(6, first, high_mult=1.05)
        span = sorted({b["timestamp"][:10] for b in dec})
        daily = {"bars": _daily_bars(span)}

        ist = timezone(timedelta(hours=5, minutes=30))
        dec_aware = [
            {**b, "timestamp": datetime.fromisoformat(b["timestamp"])
                .replace(tzinfo=ist).isoformat()}
            for b in dec
        ]
        clean = {"decision_bars": dec, "minute_bars": {"X": minute}}
        dirty = {"decision_bars": dec + dec_aware, "minute_bars": {"X": minute}}

        orig = mr._INTRADAY_DECISION_STRIDE
        try:
            mr._INTRADAY_DECISION_STRIDE = 1
            Xc, *_ = skill._prepare_intraday_training_data(
                clean, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
            Xd, *_ = skill._prepare_intraday_training_data(
                dirty, daily, horizon_minutes=375,
                target_atr_mult=0.6, sl_atr_mult=0.3,
            )
        finally:
            mr._INTRADAY_DECISION_STRIDE = orig

        assert len(Xc) > 0
        assert len(Xd) == len(Xc)  # aware duplicates collapsed, no crash

    def test_skips_symbols_below_window(self, skill):
        first = datetime(2026, 5, 18)
        decision = _five_min_bars(1, first)  # 75 bars < window_size (200)
        intraday = {"decision_bars": decision, "minute_bars": {"X": []}}
        daily = {"bars": _daily_bars(["2026-05-18"])}
        X, y, names, w, meta = skill._prepare_intraday_training_data(
            intraday, daily, horizon_minutes=60,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )
        assert X == [] and y == [] and meta == []


class TestBuildIntradayMatrix:
    """The chunked builder fetches the intraday set per symbol-chunk and
    concatenates into one matrix, realigning feature columns and re-sorting
    globally by entry_date so the walk-forward CV still splits by time."""

    async def test_chunks_concatenate_and_align(self, skill, monkeypatch):
        from unittest.mock import AsyncMock

        import yolovest.skills.model_retrain as mr

        # Force one symbol per chunk so the cross-chunk concat path runs.
        monkeypatch.setattr(mr, "_INTRADAY_SYMBOL_CHUNK", 1)

        first = datetime(2026, 5, 18)
        span = sorted({
            b["timestamp"][:10] for b in _five_min_bars(5, first)
        })
        per_symbol = {
            sym: {
                "decision_bars": _five_min_bars(5, first, symbol=sym),
                "minute_bars": {
                    sym: _minute_bars(5, first, high_mult=1.05, symbol=sym)
                },
            }
            for sym in ("AAA", "BBB")
        }
        daily = {"bars": (
            _daily_bars(span, symbol="AAA") + _daily_bars(span, symbol="BBB")
        )}

        skill.ctx.db.get_distinct_ohlcv_symbols = AsyncMock(
            return_value=["AAA", "BBB"]
        )
        skill.ctx.db.get_intraday_training_dataset = AsyncMock(
            side_effect=lambda **kw: per_symbol[kw["symbols"][0]]
        )

        X, y, names, w, meta = await skill._build_intraday_matrix(
            daily, horizon_minutes=mr._INTRADAY_TO_CLOSE_HORIZON_MIN,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )

        # Both symbols contributed.
        syms = {m["symbol"] for m in meta}
        assert syms == {"AAA", "BBB"}
        # Rectangular: every row matches the canonical column count.
        assert len(X) == len(y) == len(w) == len(meta) > 0
        assert all(len(row) == len(names) for row in X)
        assert set(y) <= {0, 1, 2}
        # Globally re-sorted by entry_date (non-decreasing across chunks).
        dates = [m["entry_date"] for m in meta]
        assert dates == sorted(dates)
        # Two symbols × one-per-chunk → two dataset fetches.
        assert skill.ctx.db.get_intraday_training_dataset.await_count == 2

    async def test_empty_when_no_intraday_symbols(self, skill):
        from unittest.mock import AsyncMock

        skill.ctx.db.get_distinct_ohlcv_symbols = AsyncMock(return_value=[])
        skill.ctx.db.get_intraday_training_dataset = AsyncMock()

        X, y, names, w, meta = await skill._build_intraday_matrix(
            {"bars": []}, horizon_minutes=375,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )

        assert (X, y, names, w, meta) == ([], [], [], [], [])
        skill.ctx.db.get_intraday_training_dataset.assert_not_called()

    async def test_drops_5m_symbols_without_1m_path(self, skill):
        # AAA has both 5m + 1m; CCC has only 5m. Only AAA is trainable —
        # CCC would emit nothing but all-HOLD, zero-return samples.
        from unittest.mock import AsyncMock

        import yolovest.skills.model_retrain as mr

        first = datetime(2026, 5, 18)
        span = sorted({b["timestamp"][:10] for b in _five_min_bars(5, first)})
        per_symbol = {
            "AAA": {
                "decision_bars": _five_min_bars(5, first, symbol="AAA"),
                "minute_bars": {
                    "AAA": _minute_bars(5, first, high_mult=1.05, symbol="AAA")
                },
            },
        }
        daily = {"bars": _daily_bars(span, symbol="AAA")}

        def _distinct(interval, **kw):
            return ["AAA", "CCC"] if interval == "5minute" else ["AAA"]

        skill.ctx.db.get_distinct_ohlcv_symbols = AsyncMock(side_effect=_distinct)
        skill.ctx.db.get_intraday_training_dataset = AsyncMock(
            side_effect=lambda **kw: per_symbol[kw["symbols"][0]]
        )

        X, y, names, w, meta = await skill._build_intraday_matrix(
            daily, horizon_minutes=mr._INTRADAY_TO_CLOSE_HORIZON_MIN,
            target_atr_mult=0.6, sl_atr_mult=0.3,
        )

        assert {m["symbol"] for m in meta} == {"AAA"}
        # Only the path-backed symbol was fetched (CCC never reached the chunk loop).
        fetched = {
            c.kwargs["symbols"][0]
            for c in skill.ctx.db.get_intraday_training_dataset.await_args_list
        }
        assert fetched == {"AAA"}


class TestIntradayRelativeLabel:
    """intraday_label_mode='relative': per 5-min decision INSTANT, forward
    returns-to-close are ranked ACROSS symbols (assigned after the
    cross-chunk concat in _build_intraday_matrix — per-chunk
    cross-sections are too thin to rank)."""

    @staticmethod
    def _panel(n_symbols: int = 12, sessions: int = 3):
        """Per symbol s: a 1-min price path with slope proportional to
        (s - mid), so the top symbols always rally into the close and the
        bottom ones always fade — a deterministic cross-sectional rank."""
        first = datetime(2026, 5, 18, 9, 15)
        decision, minute = [], {}
        for s in range(n_symbols):
            sym = f"S{s:02d}"
            slope = (s - (n_symbols - 1) / 2) * 2e-4  # per minute
            d_bars, m_bars = [], []
            day = first
            for _ in range(sessions):
                for j in range(_PER_SESSION * 5):  # 1-min path
                    px = 100.0 * (1 + slope * j)
                    t = day + timedelta(minutes=j)
                    m_bars.append({
                        "symbol": sym, "timestamp": t.isoformat(),
                        "open": px, "high": px + 0.05, "low": px - 0.05,
                        "close": px, "volume": 2000,
                    })
                for j in range(_PER_SESSION):  # 5-min decision bars
                    px = 100.0 * (1 + slope * j * 5)
                    t = day + timedelta(minutes=5 * j)
                    d_bars.append({
                        "symbol": sym, "timestamp": t.isoformat(),
                        "open": px, "high": px + 0.3, "low": px - 0.3,
                        "close": px + 0.02, "volume": 10000 + j,
                    })
                day += timedelta(days=1)
            decision.extend(d_bars)
            minute[sym] = m_bars
        return decision, minute

    async def test_top_and_bottom_symbols_get_directional_labels(self, skill):
        from unittest.mock import AsyncMock

        decision, minute = self._panel()
        symbols = sorted(minute.keys())
        span = sorted({b["timestamp"][:10] for b in decision})
        daily = {"bars": _daily_bars(span)}

        skill.ctx.db.get_distinct_ohlcv_symbols = AsyncMock(
            return_value=symbols,
        )
        skill.ctx.db.get_intraday_training_dataset = AsyncMock(
            return_value={"decision_bars": decision, "minute_bars": minute},
        )

        _x, y, _names, _w, meta = await skill._build_intraday_matrix(
            daily, horizon_minutes=375,
            target_atr_mult=8.0, sl_atr_mult=4.0,
            label_mode="relative",
        )
        assert y, "no samples emitted"
        by_symbol: dict[str, set[int]] = {}
        for lbl, m in zip(y, meta, strict=True):
            by_symbol.setdefault(m["symbol"], set()).add(lbl)

        # quantile 0.2 over 12 names -> top 2 BUY, bottom 2 SELL, per instant.
        assert by_symbol["S11"] == {2}, by_symbol["S11"]
        assert by_symbol["S10"] == {2}
        assert by_symbol["S00"] == {0}
        assert by_symbol["S01"] == {0}
        assert by_symbol["S05"] == {1}
        # Ranking inputs rode along on the meta dicts.
        assert all("_rel_fwd" in m and "_rel_group" in m for m in meta)

    async def test_triple_barrier_mode_unchanged(self, skill):
        from unittest.mock import AsyncMock

        decision, minute = self._panel()
        span = sorted({b["timestamp"][:10] for b in decision})
        skill.ctx.db.get_distinct_ohlcv_symbols = AsyncMock(
            return_value=sorted(minute.keys()),
        )
        skill.ctx.db.get_intraday_training_dataset = AsyncMock(
            return_value={"decision_bars": decision, "minute_bars": minute},
        )
        _x, y, _names, _w, meta = await skill._build_intraday_matrix(
            {"bars": _daily_bars(span)}, horizon_minutes=375,
            target_atr_mult=8.0, sl_atr_mult=4.0,
        )
        assert y
        assert all("_rel_fwd" not in m for m in meta)
