"""Tests for AppContext and MarketHoursChecker in context.py.

Tests market hours detection, holiday checking, and context attributes.
"""

from datetime import date, datetime

import pytest

from yolovest.context import MarketHoursChecker


@pytest.fixture
def market_hours_checker(sample_config) -> MarketHoursChecker:
    return MarketHoursChecker(sample_config)


class TestMarketHoursCheckerDuringHours:
    def test_is_market_hours_during_trading(self, market_hours_checker):
        # Wednesday 10:00 AM — during market hours
        dt = datetime(2026, 3, 18, 10, 0)
        assert market_hours_checker.is_market_hours(dt) is True

    def test_is_market_hours_at_open(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 9, 15)
        assert market_hours_checker.is_market_hours(dt) is True

    def test_is_market_hours_at_close(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 15, 30)
        assert market_hours_checker.is_market_hours(dt) is True

    def test_is_market_hours_midday(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 12, 0)
        assert market_hours_checker.is_market_hours(dt) is True


class TestMarketHoursCheckerOutsideHours:
    def test_is_not_market_hours_before_open(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 9, 0)
        assert market_hours_checker.is_market_hours(dt) is False

    def test_is_not_market_hours_after_close(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 15, 31)
        assert market_hours_checker.is_market_hours(dt) is False

    def test_is_not_market_hours_on_saturday(self, market_hours_checker):
        dt = datetime(2026, 3, 21, 10, 0)  # Saturday
        assert market_hours_checker.is_market_hours(dt) is False

    def test_is_not_market_hours_on_sunday(self, market_hours_checker):
        dt = datetime(2026, 3, 22, 10, 0)  # Sunday
        assert market_hours_checker.is_market_hours(dt) is False

    def test_is_not_market_hours_late_night(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 23, 0)
        assert market_hours_checker.is_market_hours(dt) is False


class TestMarketHoursCheckerHolidays:
    def test_is_holiday_on_configured_holiday(self, market_hours_checker):
        assert market_hours_checker.is_holiday(date(2026, 1, 26)) is True

    def test_is_holiday_on_independence_day(self, market_hours_checker):
        assert market_hours_checker.is_holiday(date(2026, 8, 15)) is True

    def test_is_not_holiday_on_regular_day(self, market_hours_checker):
        assert market_hours_checker.is_holiday(date(2026, 3, 18)) is False

    def test_is_not_market_hours_on_holiday(self, market_hours_checker):
        # Republic Day during normal trading hours
        dt = datetime(2026, 1, 26, 10, 0)
        assert market_hours_checker.is_market_hours(dt) is False


class TestOrderWindow:
    def test_is_order_window_during_window(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 10, 0)
        assert market_hours_checker.is_order_window(dt) is True

    def test_is_not_order_window_after_end(self, market_hours_checker):
        dt = datetime(2026, 3, 18, 15, 16)
        assert market_hours_checker.is_order_window(dt) is False

    def test_is_not_order_window_on_holiday(self, market_hours_checker):
        dt = datetime(2026, 1, 26, 10, 0)
        assert market_hours_checker.is_order_window(dt) is False

    def test_is_not_order_window_on_weekend(self, market_hours_checker):
        dt = datetime(2026, 3, 21, 10, 0)  # Saturday
        assert market_hours_checker.is_order_window(dt) is False


class TestAppContextAttributes:
    def test_context_has_config(self, app_context):
        assert app_context.config is not None
        assert app_context.config.mode == "paper"

    def test_context_has_db(self, app_context):
        assert app_context.db is not None

    def test_context_has_broker(self, app_context):
        assert app_context.broker is not None

    def test_context_has_llm(self, app_context):
        assert app_context.llm is not None

    def test_context_has_market_data(self, app_context):
        assert app_context.market_data is not None

    def test_context_has_notify(self, app_context):
        assert app_context.notify is not None

    def test_context_has_market_hours(self, app_context):
        assert app_context.market_hours is not None
        assert isinstance(app_context.market_hours, MarketHoursChecker)

    def test_context_has_event_bus(self, app_context):
        assert app_context.event_bus is not None
