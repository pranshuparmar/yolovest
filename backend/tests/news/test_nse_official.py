"""Tests for NSE official data source.

All HTTP calls are mocked via aiohttp test utilities — no real network access.
Covers: corporate announcements, corporate actions, bulk/block deals,
FII/DII activity, delivery data, health check, cookie initialization,
and graceful failure handling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from yolovest.news.nse_official import NSEOfficialSource


@pytest.fixture(autouse=True)
def _clear_endpoint_failure_cache():
    """The per-endpoint circuit breaker is module-level state. A test
    that exercises a non-200 path would otherwise poison the cache and
    make later tests' _api_get short-circuit (returning None without
    ever calling session.get). Clear it before every test."""
    from yolovest.news import nse_official
    nse_official._endpoint_failures.clear()
    yield
    nse_official._endpoint_failures.clear()


# ---------------------------------------------------------------------------
# Helpers — mock aiohttp responses
# ---------------------------------------------------------------------------


def _mock_response(
    status: int = 200,
    json_data: Any = None,
    raise_on_enter: Exception | None = None,
) -> MagicMock:
    """Create a mock aiohttp response context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.read = AsyncMock(return_value=b"")

    ctx = MagicMock()
    if raise_on_enter:
        ctx.__aenter__ = AsyncMock(side_effect=raise_on_enter)
    else:
        ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _mock_session(
    responses: dict[str, MagicMock] | None = None,
    default_status: int = 200,
    default_json: Any = None,
) -> MagicMock:
    """Create a mock aiohttp.ClientSession.

    Args:
        responses: Map of URL substrings to mock response context managers.
        default_status: Default status for unmatched URLs.
        default_json: Default JSON for unmatched URLs.
    """
    session = MagicMock(spec=aiohttp.ClientSession)
    session.close = AsyncMock()

    responses = responses or {}

    def get_side_effect(url: str, **kwargs: Any) -> MagicMock:
        for pattern, mock_resp in responses.items():
            if pattern in url:
                return mock_resp
        return _mock_response(status=default_status, json_data=default_json)

    session.get = MagicMock(side_effect=get_side_effect)
    return session


# ---------------------------------------------------------------------------
# Sample NSE API response fixtures
# ---------------------------------------------------------------------------

SAMPLE_ANNOUNCEMENTS = [
    {
        "symbol": "RELIANCE",
        "subject": "Board Meeting Outcome for Dividend",
        "desc": "Reliance Industries Limited has informed about Board Meeting Outcome",
        "an_dt": "22-Mar-2026",
    },
    {
        "symbol": "TCS",
        "subject": "Quarterly Results",
        "desc": "TCS Q4 Results",
        "an_dt": "15-Mar-2026",
    },
    {
        "symbol": "",
        "subject": "",
        "desc": "",
        "an_dt": "",
    },
]

SAMPLE_CORP_ACTIONS = [
    {
        "symbol": "RELIANCE",
        "subject": "Dividend - Rs 8 Per Share",
        "exDate": "15-Apr-2026",
        "recDate": "16-Apr-2026",
        "series": "EQ",
        "faceVal": 10,
    },
    {
        "symbol": "RELIANCE",
        "subject": "Stock Split From Rs 10/- To Rs 5/-",
        "exDate": "01-May-2026",
        "recDate": "02-May-2026",
        "series": "EQ",
        "faceVal": 10,
    },
    {
        "symbol": "RELIANCE",
        "subject": "Bonus issue 1:1",
        "exDate": "10-Jun-2026",
        "recDate": "11-Jun-2026",
        "series": "EQ",
        "faceVal": 10,
    },
    {
        "symbol": "RELIANCE",
        "subject": "Annual General Meeting",
        "exDate": "20-Jul-2026",
        "recDate": "",
        "series": "EQ",
        "faceVal": 10,
    },
]

SAMPLE_BLOCK_DEALS = {
    "data": [
        {
            "symbol": "HDFC",
            "clientName": "Goldman Sachs",
            "buySell": "BUY",
            "quantity": 500000,
            "tradePrice": 1650.50,
        },
    ]
}

SAMPLE_BULK_DEALS = {
    "data": [
        {
            "symbol": "INFY",
            "clientName": "Morgan Stanley",
            "buySell": "SELL",
            "qty": 1000000,
            "weightedAvgPrice": 1480.25,
        },
    ]
}

SAMPLE_FII_DII = [
    {
        "category": "FII/FPI *",
        "date": "22-Mar-2026",
        "buyValue": "12,345.67",
        "sellValue": "10,234.56",
        "netValue": "2,111.11",
    },
    {
        "category": "DII *",
        "date": "22-Mar-2026",
        "buyValue": "8,765.43",
        "sellValue": "9,876.54",
        "netValue": "-1,111.11",
    },
]

SAMPLE_QUOTE_EQUITY = {
    "securityWiseDP": {
        "deliveryToTradedQuantity": "45.67",
    },
    "info": {
        "symbol": "RELIANCE",
        "companyName": "Reliance Industries Limited",
    },
}


# ---------------------------------------------------------------------------
# Cookie initialization
# ---------------------------------------------------------------------------


class TestCookieInitialization:
    async def test_cookies_initialized_on_first_get_session(self):
        session = _mock_session(
            responses={"nseindia.com": _mock_response(status=200)},
        )
        source = NSEOfficialSource(session=session)
        assert not source._cookies_initialized

        result = await source._get_session()
        assert result is session
        assert source._cookies_initialized
        # Warmup is a two-hop sequence: homepage + market-data page.
        assert session.get.call_count == 2

    async def test_cookies_not_reinitialized_if_already_set(self):
        session = _mock_session()
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        await source._get_session()
        session.get.assert_not_called()

    async def test_cookies_failure_sets_flag_false(self):
        session = _mock_session(
            responses={"nseindia.com": _mock_response(status=403)},
        )
        source = NSEOfficialSource(session=session)

        await source._get_session()
        assert not source._cookies_initialized

    async def test_cookies_network_error_handled(self):
        session = MagicMock(spec=aiohttp.ClientSession)
        session.close = AsyncMock()
        session.get = MagicMock(
            return_value=_mock_response(
                raise_on_enter=aiohttp.ClientError("Connection refused"),
            ),
        )
        source = NSEOfficialSource(session=session)

        await source._get_session()
        assert not source._cookies_initialized

    async def test_creates_own_session_if_none_provided(self):
        source = NSEOfficialSource(session=None)
        assert source._owns_session is True
        assert source._session is None

        # Build the mock session before patching to avoid spec-on-mock error
        mock_sess = MagicMock()
        mock_sess.close = AsyncMock()
        mock_sess.get = MagicMock(
            return_value=_mock_response(status=200),
        )

        with patch("yolovest.news.nse_official.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_sess
            result = await source._get_session()
            assert result is mock_sess
            mock_cls.assert_called_once()


# ---------------------------------------------------------------------------
# _api_get
# ---------------------------------------------------------------------------


class TestApiGet:
    async def test_successful_json_response(self):
        expected = {"key": "value"}
        session = _mock_session(
            responses={"/api/test": _mock_response(json_data=expected)},
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source._api_get("/api/test")
        assert result == expected

    async def test_non_200_returns_none(self):
        session = _mock_session(
            responses={"/api/test": _mock_response(status=403)},
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source._api_get("/api/test")
        assert result is None

    async def test_timeout_returns_none(self):
        session = MagicMock(spec=aiohttp.ClientSession)
        session.close = AsyncMock()
        session.get = MagicMock(
            return_value=_mock_response(
                raise_on_enter=TimeoutError(),
            ),
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source._api_get("/api/test")
        assert result is None

    async def test_client_error_returns_none(self):
        session = MagicMock(spec=aiohttp.ClientSession)
        session.close = AsyncMock()
        session.get = MagicMock(
            return_value=_mock_response(
                raise_on_enter=aiohttp.ClientError("Connection reset"),
            ),
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source._api_get("/api/test")
        assert result is None

    async def test_params_passed_through(self):
        session = _mock_session()
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        await source._api_get("/api/test", params={"symbol": "TCS"})
        call_kwargs = session.get.call_args
        assert call_kwargs.kwargs.get("params") == {"symbol": "TCS"}


# ---------------------------------------------------------------------------
# fetch_headlines — corporate announcements
# ---------------------------------------------------------------------------


class TestFetchHeadlines:
    async def test_parses_announcements_into_news_articles(self):
        session = _mock_session(
            responses={
                "corporate-announcements": _mock_response(
                    json_data=SAMPLE_ANNOUNCEMENTS,
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        articles = await source.fetch_headlines(["RELIANCE", "TCS"])

        # Third item has empty subject+desc, should be skipped
        assert len(articles) == 2
        assert articles[0].source == "nse"
        assert "RELIANCE" in articles[0].headline
        assert "Dividend" in articles[0].headline
        assert articles[0].symbols == ["RELIANCE"]
        assert articles[0].published_at == datetime(2026, 3, 22)
        assert articles[0].content_hash  # auto-computed

        assert articles[1].symbols == ["TCS"]

    async def test_returns_empty_on_api_failure(self):
        session = _mock_session(
            responses={
                "corporate-announcements": _mock_response(status=403),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        articles = await source.fetch_headlines(["RELIANCE"])
        assert articles == []

    async def test_returns_empty_on_non_list_response(self):
        session = _mock_session(
            responses={
                "corporate-announcements": _mock_response(
                    json_data={"error": "not found"},
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        articles = await source.fetch_headlines(["RELIANCE"])
        assert articles == []

    async def test_symbol_matched_from_headline_when_no_symbol_field(self):
        announcements = [
            {
                "symbol": "",
                "subject": "RELIANCE board to meet on Monday",
                "desc": "",
                "an_dt": "22-Mar-2026",
            },
        ]
        session = _mock_session(
            responses={
                "corporate-announcements": _mock_response(
                    json_data=announcements,
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        articles = await source.fetch_headlines(["RELIANCE"])
        assert len(articles) == 1
        assert "RELIANCE" in articles[0].symbols

    async def test_url_is_set(self):
        session = _mock_session(
            responses={
                "corporate-announcements": _mock_response(
                    json_data=[SAMPLE_ANNOUNCEMENTS[0]],
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        articles = await source.fetch_headlines(["RELIANCE"])
        assert articles[0].url is not None
        assert "corporate-filings" in articles[0].url


# ---------------------------------------------------------------------------
# fetch_corp_actions
# ---------------------------------------------------------------------------


class TestFetchCorpActions:
    async def test_parses_dividend_split_bonus(self):
        session = _mock_session(
            responses={
                "corporateActions": _mock_response(
                    json_data=SAMPLE_CORP_ACTIONS,
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        actions = await source.fetch_corp_actions("RELIANCE")

        assert len(actions) == 4
        assert actions[0]["action_type"] == "dividend"
        assert actions[0]["symbol"] == "RELIANCE"
        assert actions[0]["ex_date"] == "15-Apr-2026"

        assert actions[1]["action_type"] == "split"
        assert actions[2]["action_type"] == "bonus"
        assert actions[3]["action_type"] == "other"

    async def test_returns_empty_on_failure(self):
        session = _mock_session(
            responses={
                "corporateActions": _mock_response(status=500),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        actions = await source.fetch_corp_actions("RELIANCE")
        assert actions == []

    async def test_returns_empty_on_non_list(self):
        session = _mock_session(
            responses={
                "corporateActions": _mock_response(
                    json_data={"error": "invalid"},
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        actions = await source.fetch_corp_actions("RELIANCE")
        assert actions == []

    async def test_passes_symbol_param(self):
        session = _mock_session(
            responses={
                "corporateActions": _mock_response(json_data=[]),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        await source.fetch_corp_actions("TCS")
        call_kwargs = session.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params.get("symbol") == "TCS"


# ---------------------------------------------------------------------------
# fetch_bulk_deals
# ---------------------------------------------------------------------------


class TestFetchBulkDeals:
    async def test_parses_block_and_bulk_deals(self):
        session = _mock_session(
            responses={
                "block-deal": _mock_response(json_data=SAMPLE_BLOCK_DEALS),
                "bulk-deal": _mock_response(json_data=SAMPLE_BULK_DEALS),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        deals = await source.fetch_bulk_deals()

        assert len(deals) == 2

        block = [d for d in deals if d["deal_type"] == "block"]
        assert len(block) == 1
        assert block[0]["symbol"] == "HDFC"
        assert block[0]["client_name"] == "Goldman Sachs"
        assert block[0]["buy_sell"] == "BUY"
        assert block[0]["quantity"] == 500000
        assert block[0]["trade_price"] == 1650.50

        bulk = [d for d in deals if d["deal_type"] == "bulk"]
        assert len(bulk) == 1
        assert bulk[0]["symbol"] == "INFY"
        assert bulk[0]["client_name"] == "Morgan Stanley"
        assert bulk[0]["quantity"] == 1000000
        assert bulk[0]["trade_price"] == 1480.25

    async def test_handles_empty_data(self):
        session = _mock_session(
            responses={
                "block-deal": _mock_response(json_data={"data": []}),
                "bulk-deal": _mock_response(json_data={"data": []}),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        deals = await source.fetch_bulk_deals()
        assert deals == []

    async def test_handles_api_failure_gracefully(self):
        session = _mock_session(
            responses={
                "block-deal": _mock_response(status=503),
                "bulk-deal": _mock_response(status=503),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        deals = await source.fetch_bulk_deals()
        assert deals == []

    async def test_partial_failure_returns_available_data(self):
        session = _mock_session(
            responses={
                "block-deal": _mock_response(json_data=SAMPLE_BLOCK_DEALS),
                "bulk-deal": _mock_response(status=500),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        deals = await source.fetch_bulk_deals()
        assert len(deals) == 1
        assert deals[0]["deal_type"] == "block"


# ---------------------------------------------------------------------------
# fetch_fii_dii
# ---------------------------------------------------------------------------


class TestFetchFiiDii:
    async def test_parses_fii_dii_data(self):
        session = _mock_session(
            responses={
                "fiidiiTradeReact": _mock_response(json_data=SAMPLE_FII_DII),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source.fetch_fii_dii()

        assert result["date"] == "22-Mar-2026"
        assert result["fii"]["buy_value"] == 12345.67
        assert result["fii"]["sell_value"] == 10234.56
        assert result["fii"]["net_value"] == 2111.11
        assert result["dii"]["buy_value"] == 8765.43
        assert result["dii"]["sell_value"] == 9876.54
        assert result["dii"]["net_value"] == -1111.11

    async def test_returns_empty_on_failure(self):
        session = _mock_session(
            responses={
                "fiidiiTradeReact": _mock_response(status=403),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source.fetch_fii_dii()
        assert result == {}

    async def test_returns_empty_on_empty_list(self):
        session = _mock_session(
            responses={
                "fiidiiTradeReact": _mock_response(json_data=[]),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source.fetch_fii_dii()
        assert result == {}

    async def test_handles_non_list_response(self):
        session = _mock_session(
            responses={
                "fiidiiTradeReact": _mock_response(
                    json_data={"error": "no data"},
                ),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        result = await source.fetch_fii_dii()
        assert result == {}


# ---------------------------------------------------------------------------
# fetch_delivery_data
# ---------------------------------------------------------------------------


class TestFetchDeliveryData:
    async def test_extracts_delivery_percentage(self):
        session = _mock_session(
            responses={
                "quote-equity": _mock_response(json_data=SAMPLE_QUOTE_EQUITY),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        pct = await source.fetch_delivery_data("RELIANCE")
        assert pct == pytest.approx(45.67)

    async def test_fallback_to_preopen_market(self):
        data = {
            "preOpenMarket": {
                "deliveryToTradedQuantity": "62.5",
            },
        }
        session = _mock_session(
            responses={
                "quote-equity": _mock_response(json_data=data),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        pct = await source.fetch_delivery_data("TCS")
        assert pct == pytest.approx(62.5)

    async def test_returns_none_on_missing_field(self):
        session = _mock_session(
            responses={
                "quote-equity": _mock_response(json_data={"info": {}}),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        pct = await source.fetch_delivery_data("RELIANCE")
        assert pct is None

    async def test_returns_none_on_api_failure(self):
        session = _mock_session(
            responses={
                "quote-equity": _mock_response(status=403),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        pct = await source.fetch_delivery_data("RELIANCE")
        assert pct is None

    async def test_passes_symbol_param(self):
        session = _mock_session(
            responses={
                "quote-equity": _mock_response(json_data=SAMPLE_QUOTE_EQUITY),
            },
        )
        source = NSEOfficialSource(session=session)
        source._cookies_initialized = True

        await source.fetch_delivery_data("INFY")
        call_kwargs = session.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params.get("symbol") == "INFY"


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_returns_true_when_cookies_initialized(self):
        session = _mock_session(
            responses={"nseindia.com": _mock_response(status=200)},
        )
        source = NSEOfficialSource(session=session)
        # Health check lazy-warms the session, then reports based on the
        # cookie state. A two-hop 200 sequence yields True.
        assert await source.health_check() is True

    async def test_returns_false_when_warmup_failed(self):
        session = _mock_session(
            responses={"nseindia.com": _mock_response(status=403)},
        )
        source = NSEOfficialSource(session=session)

        assert await source.health_check() is False

    async def test_returns_false_on_network_error(self):
        session = MagicMock(spec=aiohttp.ClientSession)
        session.close = AsyncMock()
        session.get = MagicMock(
            return_value=_mock_response(
                raise_on_enter=aiohttp.ClientError("DNS resolution failed"),
            ),
        )
        source = NSEOfficialSource(session=session)

        assert await source.health_check() is False

    async def test_returns_false_on_timeout(self):
        session = MagicMock(spec=aiohttp.ClientSession)
        session.close = AsyncMock()
        session.get = MagicMock(
            return_value=_mock_response(
                raise_on_enter=TimeoutError(),
            ),
        )
        source = NSEOfficialSource(session=session)

        assert await source.health_check() is False

    async def test_short_circuits_after_warmup_already_failed(self):
        session = _mock_session()
        source = NSEOfficialSource(session=session)
        source._cookies_failed = True

        assert await source.health_check() is False
        # Should not have touched the network.
        session.get.assert_not_called()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_closes_owned_session(self):
        session = _mock_session()
        source = NSEOfficialSource(session=session)
        source._owns_session = True
        source._cookies_initialized = True

        await source.close()
        session.close.assert_awaited_once()
        assert source._session is None
        assert not source._cookies_initialized

    async def test_does_not_close_external_session(self):
        session = _mock_session()
        source = NSEOfficialSource(session=session)
        source._owns_session = False

        await source.close()
        session.close.assert_not_awaited()
        assert source._session is not None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_announcement_headline_with_symbol_and_subject(self):
        item = {"symbol": "RELIANCE", "subject": "Board Meeting", "desc": "Details"}
        result = NSEOfficialSource._extract_announcement_headline(item)
        assert result == "RELIANCE: Board Meeting"

    def test_extract_announcement_headline_with_symbol_and_desc_only(self):
        item = {"symbol": "TCS", "subject": "", "desc": "TCS Announcement"}
        result = NSEOfficialSource._extract_announcement_headline(item)
        assert result == "TCS: TCS Announcement"

    def test_extract_announcement_headline_subject_only(self):
        item = {"symbol": "", "subject": "Market Update", "desc": ""}
        result = NSEOfficialSource._extract_announcement_headline(item)
        assert result == "Market Update"

    def test_extract_announcement_headline_desc_only(self):
        item = {"symbol": "", "subject": "", "desc": "Some description"}
        result = NSEOfficialSource._extract_announcement_headline(item)
        assert result == "Some description"

    def test_extract_announcement_headline_all_empty(self):
        item = {"symbol": "", "subject": "", "desc": ""}
        result = NSEOfficialSource._extract_announcement_headline(item)
        assert result == ""

    def test_parse_nse_date_dd_mon_yyyy(self):
        result = NSEOfficialSource._parse_nse_date("22-Mar-2026")
        assert result == datetime(2026, 3, 22)

    def test_parse_nse_date_dd_mm_yyyy(self):
        result = NSEOfficialSource._parse_nse_date("22-03-2026")
        assert result == datetime(2026, 3, 22)

    def test_parse_nse_date_iso_format(self):
        result = NSEOfficialSource._parse_nse_date("2026-03-22")
        assert result == datetime(2026, 3, 22)

    def test_parse_nse_date_slash_format(self):
        result = NSEOfficialSource._parse_nse_date("22/03/2026")
        assert result == datetime(2026, 3, 22)

    def test_parse_nse_date_with_time(self):
        result = NSEOfficialSource._parse_nse_date("22-Mar-2026 14:30")
        assert result == datetime(2026, 3, 22, 14, 30)

    def test_parse_nse_date_invalid(self):
        assert NSEOfficialSource._parse_nse_date("invalid") is None

    def test_parse_nse_date_none(self):
        assert NSEOfficialSource._parse_nse_date(None) is None

    def test_parse_nse_date_empty_string(self):
        assert NSEOfficialSource._parse_nse_date("") is None

    def test_normalize_deal_block(self):
        item = {
            "symbol": "HDFC",
            "clientName": "Goldman Sachs",
            "buySell": "BUY",
            "quantity": 500000,
            "tradePrice": 1650.50,
        }
        result = NSEOfficialSource._normalize_deal(item, "block")
        assert result["symbol"] == "HDFC"
        assert result["deal_type"] == "block"
        assert result["client_name"] == "Goldman Sachs"
        assert result["buy_sell"] == "BUY"
        assert result["quantity"] == 500000
        assert result["trade_price"] == 1650.50

    def test_normalize_deal_bulk_with_alt_fields(self):
        item = {
            "symbol": "INFY",
            "clientName": "Morgan Stanley",
            "buySell": "SELL",
            "qty": 1000000,
            "weightedAvgPrice": 1480.25,
        }
        result = NSEOfficialSource._normalize_deal(item, "bulk")
        assert result["deal_type"] == "bulk"
        assert result["quantity"] == 1000000
        assert result["trade_price"] == 1480.25

    def test_safe_float_normal(self):
        assert NSEOfficialSource._safe_float(42.5) == 42.5

    def test_safe_float_string(self):
        assert NSEOfficialSource._safe_float("123.45") == 123.45

    def test_safe_float_string_with_commas(self):
        assert NSEOfficialSource._safe_float("12,345.67") == 12345.67

    def test_safe_float_none(self):
        assert NSEOfficialSource._safe_float(None) == 0.0

    def test_safe_float_invalid(self):
        assert NSEOfficialSource._safe_float("not-a-number") == 0.0

    def test_safe_float_int(self):
        assert NSEOfficialSource._safe_float(100) == 100.0
