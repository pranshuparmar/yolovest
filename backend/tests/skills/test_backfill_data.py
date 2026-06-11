"""Tests for backfill-data skill."""

from unittest.mock import AsyncMock

import pytest

from yolovest.models.schemas import OHLCVBar
from yolovest.skills.backfill_data import BackfillDataSkill


@pytest.fixture
def backfill_skill(app_context):
    # Skip the per-symbol sleep in tests
    skill = BackfillDataSkill(app_context)
    skill._PER_SYMBOL_DELAY_SEC = 0
    return skill


@pytest.fixture
def fake_bars():
    from datetime import datetime
    return [
        OHLCVBar(
            timestamp=datetime(2026, 5, 10),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        ),
    ]


class TestSourceProvenance:
    """ohlcv.source must record the actual provider that produced the
    bars (kite/jugaad/...) so data provenance is auditable, not a static
    skill label."""

    async def test_records_winning_provider(self, backfill_skill, fake_bars):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.config.strategy.market_regime.enabled = False
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.market_data.get_fetch_meta = lambda sym: {"source": "kite"}

        await backfill_skill.execute()

        srcs = [c.args[3] for c in ctx.db.upsert_ohlcv.call_args_list]
        assert srcs and all(s == "kite" for s in srcs)

    async def test_falls_back_to_skill_label_without_meta(self, backfill_skill, fake_bars):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.config.strategy.market_regime.enabled = False
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        # Meta carries no usable source -> default skill label.
        ctx.market_data.get_fetch_meta = lambda sym: {}

        await backfill_skill.execute()

        srcs = [c.args[3] for c in ctx.db.upsert_ohlcv.call_args_list]
        assert srcs and all(s == "backfill" for s in srcs)


class TestBackfillDefaults:
    """The skill must default to *tracked* symbols, not seed_symbols."""

    async def test_uses_watchlist_user_watchlist_and_regime_index(
        self, backfill_skill, fake_bars
    ):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"}, {"symbol": "TCS"},
        ])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[
            {"symbol": "INFY"}, {"symbol": "RELIANCE"},  # dedup with watchlist
        ])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = True
        ctx.config.strategy.market_regime.index_symbol = "NIFTY 50"

        result = await backfill_skill.execute()

        assert result.success
        called_symbols = [
            call.args[0] for call in ctx.market_data.get_ohlcv.call_args_list
        ]
        # Watchlist + user_watchlist + index, deduped and sorted
        assert sorted(called_symbols) == ["INFY", "NIFTY 50", "RELIANCE", "TCS"]
        assert result.data["symbols_total"] == 4

    async def test_falls_back_to_seed_symbols_when_nothing_tracked(
        self, backfill_skill, fake_bars
    ):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = False
        ctx.config.scanning.seed_symbols = ["RELIANCE", "TCS"]

        result = await backfill_skill.execute()

        assert result.success
        called = [
            call.args[0] for call in ctx.market_data.get_ohlcv.call_args_list
        ]
        assert sorted(called) == ["RELIANCE", "TCS"]

    async def test_explicit_symbols_kwarg_overrides_defaults(
        self, backfill_skill, fake_bars
    ):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await backfill_skill.execute(symbols=["WIPRO"])

        assert result.success
        called = [
            call.args[0] for call in ctx.market_data.get_ohlcv.call_args_list
        ]
        assert called == ["WIPRO"]

    async def test_skips_regime_index_when_disabled(
        self, backfill_skill, fake_bars
    ):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = False

        result = await backfill_skill.execute()

        assert result.success
        called = [
            call.args[0] for call in ctx.market_data.get_ohlcv.call_args_list
        ]
        assert called == ["RELIANCE"]


class TestBackfillErrorHandling:
    async def test_individual_failure_does_not_abort_run(
        self, backfill_skill, fake_bars
    ):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[
            {"symbol": "RELIANCE"}, {"symbol": "BADSYMBOL"}, {"symbol": "INFY"},
        ])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.config.strategy.market_regime.enabled = False

        async def fake_get(symbol, *_args, **_kwargs):
            if symbol == "BADSYMBOL":
                raise ValueError("delisted")
            return fake_bars

        ctx.market_data.get_ohlcv = AsyncMock(side_effect=fake_get)

        result = await backfill_skill.execute()

        assert result.success
        assert result.data["symbols_processed"] == 2
        assert len(result.data["errors"]) == 1
        assert "BADSYMBOL" in result.data["errors"][0]


class TestBackfillIntervalKwarg:
    """The skill should pass the requested interval through to get_ohlcv."""

    async def test_default_interval_is_daily(self, backfill_skill, fake_bars):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = False

        await backfill_skill.execute()

        call_args = ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.args[1] == "daily"

    async def test_explicit_interval_kwarg_is_used(self, backfill_skill, fake_bars):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = False

        await backfill_skill.execute(interval="5minute")

        call_args = ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.args[1] == "5minute"
        # Source label should reflect the non-daily interval
        upsert_args = ctx.db.upsert_ohlcv.call_args_list[0]
        assert upsert_args.args[3] == "backfill_5minute"


class TestBackfillIntradaySkill:
    """The 5-minute backfill skill mirrors backfill-data with intraday defaults."""

    async def test_defaults_to_5minute_interval(self, app_context, fake_bars):
        import json

        from yolovest.skills.backfill_intraday import BackfillIntradaySkill

        skill = BackfillIntradaySkill(app_context)
        skill._PER_SYMBOL_DELAY_SEC = 0
        ctx = skill.ctx

        ctx.db.get_system_state = AsyncMock(
            return_value=json.dumps({"symbols": ["RELIANCE"]})
        )
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await skill.execute()

        assert result.success
        call_args = ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.args[1] == "5minute"
        # 1y default window — not the 3y daily backfill window
        assert result.data["days_requested"] == 365

    async def test_registered_in_skill_registry(self):
        from yolovest.skills import SKILL_REGISTRY
        from yolovest.skills.backfill_intraday import BackfillIntradaySkill

        assert SKILL_REGISTRY["backfill-intraday"] is BackfillIntradaySkill


class TestBackfillIntraday1mSkill:
    """The 1-minute label-precision backfill is deliberately bounded:
    Nifty 100 universe (not the full F&O set) and a window capped at the
    intraday retention horizon, so it can't re-bloat the operational DB."""

    async def test_defaults_to_nifty100_and_1m_interval(self, app_context, fake_bars):
        import json

        from yolovest.skills.backfill_intraday import BackfillIntraday1mSkill

        skill = BackfillIntraday1mSkill(app_context)
        skill._PER_SYMBOL_DELAY_SEC = 0
        ctx = skill.ctx

        # Constituents come from the ingest-universe cache.
        ctx.db.get_system_state = AsyncMock(
            return_value=json.dumps({"symbols": ["RELIANCE", "TCS"]})
        )
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await skill.execute()

        assert result.success
        ctx.db.get_system_state.assert_awaited_with("universe_constituents:nifty100")
        call_args = ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.args[1] == "1m"
        called = sorted(c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list)
        assert called == ["RELIANCE", "TCS"]

    async def test_window_capped_at_intraday_retention(self, app_context):
        from yolovest.skills.backfill_intraday import BackfillIntraday1mSkill

        skill = BackfillIntraday1mSkill(app_context)
        ctx = skill.ctx
        # Even with a deep 5-min backfill depth, the 1-min layer is capped
        # at the intraday retention horizon.
        ctx.config.market_data.intraday_backfill_days = 750
        ctx.config.database.retention.intraday_ohlcv_days = 365
        assert skill._default_days() == 365

    async def test_skips_when_constituents_unresolvable(
        self, app_context, fake_bars, monkeypatch
    ):
        """No cache + live fetch unavailable → do nothing rather than
        backfilling the broad ~500-name bundled list."""
        import yolovest.skills.backfill_data as bf
        from yolovest.skills.backfill_intraday import BackfillIntraday1mSkill

        skill = BackfillIntraday1mSkill(app_context)
        skill._PER_SYMBOL_DELAY_SEC = 0
        ctx = skill.ctx
        ctx.db.get_system_state = AsyncMock(return_value=None)
        monkeypatch.setattr(bf, "fetch_live_constituents", AsyncMock(return_value=None))
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await skill.execute()

        assert result.success
        ctx.market_data.get_ohlcv.assert_not_called()

    async def test_registered_in_skill_registry(self):
        from yolovest.skills import SKILL_REGISTRY
        from yolovest.skills.backfill_intraday import BackfillIntraday1mSkill

        assert SKILL_REGISTRY["backfill-intraday-1m"] is BackfillIntraday1mSkill


class TestIntradayUniverse:
    """backfill-intraday now defaults to the Nifty 100 universe (the
    intraday model's coherent tradable set); the broader F&O set stays
    reachable via universe="fno". backfill-data stays on tracked symbols."""

    @pytest.fixture
    def intraday_skill(self, app_context):
        from yolovest.skills.backfill_intraday import BackfillIntradaySkill
        skill = BackfillIntradaySkill(app_context)
        skill._PER_SYMBOL_DELAY_SEC = 0
        return skill

    async def test_intraday_defaults_to_nifty100(self, intraday_skill, fake_bars):
        import json
        ctx = intraday_skill.ctx
        ctx.db.get_system_state = AsyncMock(
            return_value=json.dumps({"symbols": ["RELIANCE", "INFY"]})
        )
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        # Should NOT consult the watchlist for an index universe
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "TCS"}])

        result = await intraday_skill.execute()

        assert result.success
        ctx.db.get_system_state.assert_awaited_with("universe_constituents:nifty100")
        called = sorted(c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list)
        assert called == ["INFY", "RELIANCE"]
        ctx.db.get_watchlist.assert_not_called()

    async def test_intraday_fno_via_live_fetch_explicit(self, intraday_skill, fake_bars):
        ctx = intraday_skill.ctx

        class FakeKite:
            def instruments(self, seg):
                return [
                    {"name": "RELIANCE"}, {"name": "INFY"},
                    {"name": "INFY"},  # dup underlying
                    {"name": "NIFTY"},  # index — excluded
                ]
        ctx.broker._kite = FakeKite()
        ctx.broker._access_token = "real_token"
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await intraday_skill.execute(universe="fno")

        assert result.success
        called = sorted(
            c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list
        )
        assert called == ["INFY", "RELIANCE"]  # deduped, index dropped

    async def test_intraday_fno_falls_back_to_fno_daily_when_unauthenticated(
        self, intraday_skill, fake_bars
    ):
        ctx = intraday_skill.ctx
        ctx.broker._kite = None  # no live client
        ctx.db.get_distinct_fno_underlyings = AsyncMock(return_value=["HDFCBANK", "ICICIBANK"])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await intraday_skill.execute(universe="fno")

        assert result.success
        called = sorted(c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list)
        assert called == ["HDFCBANK", "ICICIBANK"]

    async def test_intraday_universe_tracked_override(self, intraday_skill, fake_bars):
        ctx = intraday_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "WIPRO"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.config.strategy.market_regime.enabled = False
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await intraday_skill.execute(universe="tracked")

        assert result.success
        called = [c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list]
        assert called == ["WIPRO"]

    async def test_daily_backfill_still_defaults_tracked(self, backfill_skill, fake_bars):
        ctx = backfill_skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.config.strategy.market_regime.enabled = False
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)

        result = await backfill_skill.execute()

        assert result.success
        called = [c.args[0] for c in ctx.market_data.get_ohlcv.call_args_list]
        assert called == ["RELIANCE"]
