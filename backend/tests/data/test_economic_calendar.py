"""Tests for the economic calendar module."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

from yolovest.data.economic_calendar import EconomicCalendarSource


class TestEconomicCalendarSource:
    async def test_make_event_generates_hash(self):
        event = EconomicCalendarSource._make_event(
            event_date="2026-03-18",
            event_type="monetary_policy",
            title="US Fed FOMC Meeting",
            country="US",
            impact="high",
            source="fed_schedule",
        )
        assert event["content_hash"]
        assert len(event["content_hash"]) == 64  # SHA256 hex
        assert event["event_date"] == "2026-03-18"
        assert event["event_type"] == "monetary_policy"
        assert event["country"] == "US"

    async def test_make_event_with_symbol(self):
        event = EconomicCalendarSource._make_event(
            event_date="2026-04-15",
            event_type="earnings",
            title="TCS Board Meeting / Results",
            country="IN",
            impact="medium",
            source="nse_announcements",
            symbol="TCS",
        )
        assert event["symbol"] == "TCS"

    async def test_deduplicate_removes_dupes(self):
        events = [
            EconomicCalendarSource._make_event(
                "2026-03-18", "monetary_policy", "FOMC", "US", "high", "fed"
            ),
            EconomicCalendarSource._make_event(
                "2026-03-18", "monetary_policy", "FOMC", "US", "high", "fed"
            ),
            EconomicCalendarSource._make_event(
                "2026-04-07", "monetary_policy", "RBI MPC", "IN", "high", "rbi"
            ),
        ]
        result = EconomicCalendarSource._deduplicate(events)
        assert len(result) == 2

    async def test_fetch_fed_events_within_window(self):
        source = EconomicCalendarSource()
        try:
            # Fetch with a wide window to catch known FOMC dates
            events = await source._fetch_fed_events(lookback_days=0, lookahead_days=365)
            assert len(events) > 0
            for e in events:
                assert e["country"] == "US"
                assert e["event_type"] == "global_monetary_policy"
                assert e["impact"] == "medium"
                assert e["source"] == "fed_schedule"
        finally:
            await source.close()

    async def test_fetch_fed_events_narrow_window_may_be_empty(self):
        source = EconomicCalendarSource()
        try:
            # Very narrow window — might not contain any FOMC dates
            events = await source._fetch_fed_events(lookback_days=0, lookahead_days=1)
            # Should not error, just may be empty
            assert isinstance(events, list)
        finally:
            await source.close()

    async def test_fetch_rbi_events_includes_scheduled(self):
        source = EconomicCalendarSource()
        try:
            events = await source._fetch_rbi_events(lookback_days=0, lookahead_days=365)
            rbi_events = [e for e in events if e["country"] == "IN"]
            assert len(rbi_events) > 0
            for e in rbi_events:
                assert e["event_type"] == "monetary_policy"
        finally:
            await source.close()

    async def test_fetch_earnings_handles_network_failure(self):
        source = EconomicCalendarSource()
        try:
            # Mock the session to simulate network failure
            mock_session = MagicMock()
            mock_resp = AsyncMock()
            mock_resp.status = 500
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_session.get = MagicMock(return_value=mock_resp)
            source._session = mock_session
            source._owns_session = False

            events = await source._fetch_earnings_dates(lookback_days=0, lookahead_days=30)
            assert events == []  # Graceful failure
        finally:
            pass  # Don't close the mock session

    async def test_fetch_all_events_aggregates_sources(self):
        source = EconomicCalendarSource()

        # Mock individual fetchers
        source._fetch_rbi_events = AsyncMock(return_value=[
            EconomicCalendarSource._make_event(
                "2026-04-07", "monetary_policy", "RBI MPC", "IN", "high", "rbi_schedule"
            ),
        ])
        source._fetch_fed_events = AsyncMock(return_value=[
            EconomicCalendarSource._make_event(
                "2026-03-18", "monetary_policy", "FOMC", "US", "high", "fed_schedule"
            ),
        ])
        source._fetch_earnings_dates = AsyncMock(return_value=[
            EconomicCalendarSource._make_event(
                "2026-04-15", "earnings", "TCS Results", "IN", "medium", "nse",
                symbol="TCS",
            ),
        ])

        events = await source.fetch_all_events()
        assert len(events) == 3
        countries = {e["country"] for e in events}
        assert "IN" in countries
        assert "US" in countries

    async def test_fetch_all_events_handles_partial_failure(self):
        source = EconomicCalendarSource()

        source._fetch_rbi_events = AsyncMock(side_effect=Exception("RBI down"))
        source._fetch_fed_events = AsyncMock(return_value=[
            EconomicCalendarSource._make_event(
                "2026-03-18", "monetary_policy", "FOMC", "US", "high", "fed_schedule"
            ),
        ])
        source._fetch_earnings_dates = AsyncMock(return_value=[])

        events = await source.fetch_all_events()
        assert len(events) == 1  # Only Fed events returned
        assert events[0]["country"] == "US"

    def test_try_parse_date_formats(self):
        assert EconomicCalendarSource._try_parse_date("20-Mar-2026") == date(2026, 3, 20)
        assert EconomicCalendarSource._try_parse_date("20-03-2026") == date(2026, 3, 20)
        assert EconomicCalendarSource._try_parse_date("2026-03-20") == date(2026, 3, 20)
        assert EconomicCalendarSource._try_parse_date("20/03/2026") == date(2026, 3, 20)
        assert EconomicCalendarSource._try_parse_date("invalid") is None

    def test_parse_rbi_announcements_empty_html(self):
        events = EconomicCalendarSource._parse_rbi_announcements(
            "", date(2026, 1, 1), date(2026, 12, 31)
        )
        assert events == []


class TestEconomicEventsDB:
    """Test DB methods for economic events via the real Database class."""

    async def test_upsert_and_query_economic_events(self, tmp_path):
        from datetime import date, timedelta

        from yolovest.data.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        # Anchor event dates to the future relative to today so the
        # get_upcoming_economic_events(days=60) window actually
        # contains them — hardcoded 2026-03/04 dates fall out of the
        # window as the wall clock advances.
        today = date.today()
        d1 = (today + timedelta(days=10)).isoformat()
        d2 = (today + timedelta(days=20)).isoformat()
        d3 = (today + timedelta(days=30)).isoformat()

        try:
            events = [
                {
                    "event_date": d1,
                    "event_type": "monetary_policy",
                    "title": "US Fed FOMC Meeting",
                    "country": "US",
                    "impact": "high",
                    "source": "fed_schedule",
                    "content_hash": "abc123",
                },
                {
                    "event_date": d2,
                    "event_type": "monetary_policy",
                    "title": "RBI MPC Meeting",
                    "country": "IN",
                    "impact": "high",
                    "source": "rbi_schedule",
                    "content_hash": "def456",
                },
                {
                    "event_date": d3,
                    "event_type": "earnings",
                    "title": "TCS Board Meeting",
                    "country": "IN",
                    "impact": "medium",
                    "source": "nse_announcements",
                    "symbol": "TCS",
                    "content_hash": "ghi789",
                },
            ]

            count = await db.upsert_economic_events(events)
            assert count == 3

            # Duplicate insert should not fail
            count2 = await db.upsert_economic_events(events)
            assert count2 == 3  # All silently skipped

            # Query upcoming events
            upcoming = await db.get_upcoming_economic_events(days=60)
            assert len(upcoming) >= 2  # At least some are in the future window

            # Filter by country
            us_events = await db.get_upcoming_economic_events(days=60, country="US")
            for e in us_events:
                assert e["country"] == "US"

            # Filter by event type
            policy = await db.get_upcoming_economic_events(
                days=60, event_type="monetary_policy"
            )
            for e in policy:
                assert e["event_type"] == "monetary_policy"

            # Earnings query
            earnings = await db.get_earnings_events(days=60)
            for e in earnings:
                assert e["title"]

            # Earnings by symbol
            tcs_earnings = await db.get_earnings_events(symbol="TCS", days=60)
            for e in tcs_earnings:
                assert e["symbol"] == "TCS"

        finally:
            await db.close()

    async def test_migration_004_creates_table(self, tmp_path):
        from yolovest.data.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            cursor = await db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='economic_events'"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "economic_events"

            # Check schema version includes migration 4
            version = await db.get_schema_version()
            assert version >= 4
        finally:
            await db.close()
