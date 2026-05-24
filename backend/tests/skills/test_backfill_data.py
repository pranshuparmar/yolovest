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
        from yolovest.skills.backfill_intraday import BackfillIntradaySkill

        skill = BackfillIntradaySkill(app_context)
        skill._PER_SYMBOL_DELAY_SEC = 0
        ctx = skill.ctx
        ctx.db.get_watchlist = AsyncMock(return_value=[{"symbol": "RELIANCE"}])
        ctx.db.get_user_watchlist = AsyncMock(return_value=[])
        ctx.db.upsert_ohlcv = AsyncMock(return_value=1)
        ctx.market_data.get_ohlcv = AsyncMock(return_value=fake_bars)
        ctx.config.strategy.market_regime.enabled = False

        result = await skill.execute()

        assert result.success
        call_args = ctx.market_data.get_ohlcv.call_args_list[0]
        assert call_args.args[1] == "5minute"
        # 1y default — not the 3y daily backfill window
        assert result.data["days_requested"] == 365

    async def test_registered_in_skill_registry(self):
        from yolovest.skills import SKILL_REGISTRY
        from yolovest.skills.backfill_intraday import BackfillIntradaySkill

        assert SKILL_REGISTRY["backfill-intraday"] is BackfillIntradaySkill
