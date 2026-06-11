"""Tests for P2 features: dynamic economic calendar, early close handling,
agent memory persistence, and Kite data provider."""

import json
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from yolovest.config import AppConfig
from yolovest.context import MarketHoursChecker

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Dynamic Economic Calendar
# ---------------------------------------------------------------------------


class TestDynamicEconomicCalendar:
    def test_fomc_dates_for_2026(self):
        from yolovest.data.economic_calendar import _get_fomc_dates

        dates = _get_fomc_dates(2026)
        assert len(dates) > 0
        assert all(d.startswith("2026-") for d in dates)

    def test_fomc_dates_for_2025(self):
        from yolovest.data.economic_calendar import _get_fomc_dates

        dates = _get_fomc_dates(2025)
        assert len(dates) > 0
        assert all(d.startswith("2025-") for d in dates)

    def test_fomc_dates_unknown_year(self):
        from yolovest.data.economic_calendar import _get_fomc_dates

        dates = _get_fomc_dates(2099)
        assert dates == []

    def test_rbi_dates_for_2026(self):
        from yolovest.data.economic_calendar import _get_rbi_mpc_dates

        dates = _get_rbi_mpc_dates(2026)
        assert len(dates) > 0
        assert all(d.startswith("2026-") for d in dates)

    def test_rbi_dates_unknown_year(self):
        from yolovest.data.economic_calendar import _get_rbi_mpc_dates

        dates = _get_rbi_mpc_dates(2099)
        assert dates == []

    async def test_fed_events_uses_static_tables(self):
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        events = await source._fetch_fed_events(lookback_days=0, lookahead_days=365)

        # FOMC events should be tagged as secondary context
        for e in events:
            assert e["country"] == "US"
            assert e["impact"] == "medium"  # medium for India, not high
            assert e["event_type"] == "global_monetary_policy"

    async def test_fed_events_lower_impact_than_rbi(self):
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        # Mock RBI fetch to avoid HTTP calls
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(),
        ))
        source._session = mock_session
        source._owns_session = False

        rbi = await source._fetch_rbi_events(lookback_days=0, lookahead_days=365)
        fed = await source._fetch_fed_events(lookback_days=0, lookahead_days=365)

        # RBI should be "high" impact, Fed should be "medium"
        for e in rbi:
            assert e["impact"] == "high"
        for e in fed:
            assert e["impact"] == "medium"

    async def test_rbi_events_spans_year_boundary(self):
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 404  # Force fallback to static dates
        mock_session.get = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(),
        ))
        source._session = mock_session
        source._owns_session = False

        # This should look up dates for the relevant years
        events = await source._fetch_rbi_events(lookback_days=0, lookahead_days=365)
        # Should have RBI MPC dates
        assert all(e["country"] == "IN" for e in events)

    def test_rbi_is_primary_in_fetchers_order(self):
        """RBI should be fetched before Fed in the source list."""
        from yolovest.data.economic_calendar import EconomicCalendarSource

        source = EconomicCalendarSource()
        # Verify the fetch order in fetch_all_events
        import inspect
        src = inspect.getsource(source.fetch_all_events)
        rbi_pos = src.find("rbi")
        fed_pos = src.find("fed")
        assert rbi_pos < fed_pos, "RBI should be fetched before Fed"


# ---------------------------------------------------------------------------
# Early Close Day Handling
# ---------------------------------------------------------------------------


class TestEarlyCloseHandling:
    @pytest.fixture
    def early_close_config(self) -> AppConfig:
        return AppConfig(
            mode="paper",
            capital={"initial_amount": 100000},
            broker={"api_key": "test", "api_secret": "test"},
            llm={"model": "gemini-2.5-flash", "api_key": "test"},
            market_data={"daily_provider": "jugaad", "stale_threshold_minutes": 30},
            heartbeat={"market_hours_interval_min": 15, "off_hours_interval_min": 60},
            scanning={"seed_symbols": ["RELIANCE"]},
            risk={"max_risk_per_trade_pct": 0.02},
            market_hours={
                "open": "09:15",
                "close": "15:30",
                "order_start": "09:15",
                "order_end": "15:15",
                "square_off": "15:15",
                "timezone": "Asia/Kolkata",
                "holidays": [],
                "early_close_days": {"2026-03-23": "12:45"},
            },
            notifications={"telegram": {"enabled": False}},
        )

    def test_is_early_close_day(self, early_close_config):
        checker = MarketHoursChecker(early_close_config)
        assert checker.is_early_close_day(date(2026, 3, 23))
        assert not checker.is_early_close_day(date(2026, 3, 24))

    def test_square_off_time_on_early_close(self, early_close_config):
        checker = MarketHoursChecker(early_close_config)
        sq = checker.get_square_off_time(date(2026, 3, 23))
        assert sq == time(12, 45)

    def test_square_off_time_normal_day(self, early_close_config):
        checker = MarketHoursChecker(early_close_config)
        sq = checker.get_square_off_time(date(2026, 3, 24))
        assert sq == time(15, 15)

    def test_market_hours_respects_early_close(self, early_close_config):
        checker = MarketHoursChecker(early_close_config)
        # 13:00 on early close day (closes at 12:45) — should be outside market hours
        t = datetime(2026, 3, 23, 13, 0, tzinfo=IST)
        assert not checker.is_market_hours(t)

        # 12:30 on early close day — should be within market hours
        t = datetime(2026, 3, 23, 12, 30, tzinfo=IST)
        assert checker.is_market_hours(t)

    def test_order_window_caps_on_early_close(self, early_close_config):
        checker = MarketHoursChecker(early_close_config)
        # 12:50 on early close day — past early square-off of 12:45
        t = datetime(2026, 3, 23, 12, 50, tzinfo=IST)
        assert not checker.is_order_window(t)

        # 12:40 on early close day — before early square-off
        t = datetime(2026, 3, 23, 12, 40, tzinfo=IST)
        assert checker.is_order_window(t)

    async def test_risk_check_skips_early_close_for_cnc(self, app_context):
        """CNC orders should not be blocked by early close check."""
        from yolovest.skills.risk_check import RiskCheckSkill

        skill = RiskCheckSkill(app_context)

        signal = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500,
            "stop_loss_price": 2450,
            "target_price": 2600,
            "position_size": 10,
            "product": "CNC",  # delivery — not affected by early close
        }

        with patch.object(skill.ctx.market_hours, "is_order_window", return_value=True), \
             patch.object(skill.ctx.market_hours, "is_early_close_day", return_value=True):
            result = await skill.execute(signal=signal)
            # CNC should pass through even on early close day
            assert result.success
            assert result.data["approved"]


# ---------------------------------------------------------------------------
# Agent Memory Persistence
# ---------------------------------------------------------------------------


class TestAgentMemory:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.get_memory = AsyncMock(return_value=None)
        db.set_memory = AsyncMock()
        db.delete_memory = AsyncMock()
        db.list_memory_keys = AsyncMock(return_value=[])
        db.get_all_memory = AsyncMock(return_value=[])
        db.cleanup_expired_memory = AsyncMock(return_value=0)
        return db

    async def test_set_and_get(self, mock_db):
        from yolovest.memory import AgentMemory

        mem = AgentMemory(mock_db)

        await mem.set("session", "key1", {"foo": "bar"})
        mock_db.set_memory.assert_awaited_once()
        call_args = mock_db.set_memory.call_args
        assert call_args[0][0] == "session"
        assert call_args[0][1] == "key1"
        assert json.loads(call_args[0][2]) == {"foo": "bar"}

    async def test_get_returns_parsed_json(self, mock_db):
        from yolovest.memory import AgentMemory

        mock_db.get_memory.return_value = {
            "key": "k1",
            "value": '{"a": 1}',
            "expires_at": None,
        }
        mem = AgentMemory(mock_db)
        result = await mem.get("ns", "k1")
        assert result == {"a": 1}

    async def test_get_returns_none_for_expired(self, mock_db):
        from yolovest.memory import AgentMemory
        from yolovest.timezone import now_ist
        past = (now_ist() - timedelta(hours=1)).isoformat()
        mock_db.get_memory.return_value = {
            "key": "k1",
            "value": '"old"',
            "expires_at": past,
        }
        mem = AgentMemory(mock_db)
        result = await mem.get("ns", "k1")
        assert result is None
        mock_db.delete_memory.assert_awaited_once()

    async def test_set_with_ttl(self, mock_db):
        from yolovest.memory import AgentMemory

        mem = AgentMemory(mock_db)
        await mem.set("ns", "k1", "val", ttl_hours=2)
        call_args = mock_db.set_memory.call_args
        expires = call_args[0][3]
        assert expires is not None

    async def test_save_heartbeat_state(self, mock_db):
        from yolovest.memory import AgentMemory

        mem = AgentMemory(mock_db)
        await mem.save_heartbeat_state({"signals": 3})
        mock_db.set_memory.assert_awaited_once()
        args = mock_db.set_memory.call_args[0]
        assert args[0] == "heartbeat"
        assert args[1] == "last_result"

    async def test_get_all(self, mock_db):
        from yolovest.memory import AgentMemory

        mock_db.get_all_memory.return_value = [
            {"key": "a", "value": '"x"', "expires_at": None},
            {"key": "b", "value": "42", "expires_at": None},
        ]
        mem = AgentMemory(mock_db)
        result = await mem.get_all("ns")
        assert result == {"a": "x", "b": 42}

    async def test_cleanup_expired(self, mock_db):
        from yolovest.memory import AgentMemory

        mock_db.cleanup_expired_memory.return_value = 5
        mem = AgentMemory(mock_db)
        count = await mem.cleanup_expired()
        assert count == 5

    async def test_list_keys(self, mock_db):
        from yolovest.memory import AgentMemory

        mock_db.list_memory_keys.return_value = ["k1", "k2"]
        mem = AgentMemory(mock_db)
        keys = await mem.list_keys("ns")
        assert keys == ["k1", "k2"]

    async def test_error_handling(self, mock_db):
        from yolovest.memory import AgentMemory

        mock_db.get_memory.side_effect = Exception("DB down")
        mem = AgentMemory(mock_db)
        result = await mem.get("ns", "k1")
        assert result is None  # graceful failure


# ---------------------------------------------------------------------------
# Kite Data Provider
# ---------------------------------------------------------------------------


class TestKiteDataProvider:
    def test_interval_mapping(self):
        from yolovest.data.kite_data import _INTERVAL_MAP

        assert _INTERVAL_MAP["daily"] == "day"
        assert _INTERVAL_MAP["5minute"] == "5minute"
        assert _INTERVAL_MAP["15minute"] == "15minute"

    def test_set_access_token(self):
        from yolovest.data.kite_data import KiteDataProvider

        provider = KiteDataProvider(api_key="test_key")
        provider.set_access_token("new_token")
        assert provider._access_token == "new_token"
        assert provider._kite is None  # forces re-init

    async def test_health_check_no_token(self):
        from yolovest.data.kite_data import KiteDataProvider

        provider = KiteDataProvider(api_key="test_key")
        assert not await provider.health_check()

    async def test_get_ohlcv_unsupported_interval(self):
        from yolovest.data.kite_data import KiteDataProvider

        provider = KiteDataProvider(api_key="test_key")
        with pytest.raises(ValueError, match="Unsupported interval"):
            await provider.get_ohlcv("RELIANCE", "3minute")

    def test_build_market_data_with_kite(self, sample_config):
        """Test that kite_data_enabled adds Kite as a provider."""
        sample_config.market_data.kite_data_enabled = True
        # Without actual Kite SDK, it will fall through to jugaad
        from yolovest.main import _build_market_data

        result = _build_market_data(sample_config)
        # Should still build successfully even if kiteconnect isn't installed
        assert result is not None

    def test_build_market_data_without_kite(self, sample_config):
        """Default: Kite not enabled."""
        sample_config.market_data.kite_data_enabled = False
        from yolovest.main import _build_market_data

        result = _build_market_data(sample_config)
        assert result is not None


# ---------------------------------------------------------------------------
# Memory wiring in main.py
# ---------------------------------------------------------------------------


class TestMemoryWiring:
    def test_build_memory(self):
        from yolovest.main import _build_memory
        from yolovest.memory import AgentMemory

        mock_db = AsyncMock()
        mem = _build_memory(mock_db)
        assert isinstance(mem, AgentMemory)

    def test_context_has_memory_field(self, app_context):
        assert hasattr(app_context, "memory")
