"""Tests for Screener.in and Trendlyne scrapers."""

from unittest.mock import AsyncMock, MagicMock

from yolovest.data.screener import ScreenerScraper
from yolovest.data.trendlyne import TrendlyneScraper

# -----------------------------------------------------------------------
# Screener.in Tests
# -----------------------------------------------------------------------


class TestScreenerScraper:
    def test_parse_fundamentals_full_data(self):
        html = """
        <div>
            <span>Stock P/E</span> <span class="value">25.3</span>
            <span>Price to book value</span> <span class="value">4.2</span>
            <span>Debt to equity</span> <span class="value">0.35</span>
            <span>Promoter holding</span> <span class="value">50.1 %</span>
            <span>Sales growth</span> <span class="value">12.5 %</span>
        </div>
        """
        result = ScreenerScraper._parse_fundamentals(html, "RELIANCE")
        assert result is not None
        assert result["pe_ratio"] == 25.3
        assert result["pb_ratio"] == 4.2
        assert result["debt_to_equity"] == 0.35
        assert result["promoter_holding_pct"] == 50.1
        assert result["quarterly_revenue_growth_pct"] == 12.5

    def test_parse_fundamentals_partial_data(self):
        html = """
        <div>
            <span>Stock P/E</span> <span class="value">18.7</span>
            <span>Debt to equity</span> <span class="value">1.2</span>
        </div>
        """
        result = ScreenerScraper._parse_fundamentals(html, "TCS")
        assert result is not None
        assert result["pe_ratio"] == 18.7
        assert result["debt_to_equity"] == 1.2
        assert "pb_ratio" not in result

    def test_parse_fundamentals_empty_html(self):
        result = ScreenerScraper._parse_fundamentals("", "UNKNOWN")
        assert result is None

    def test_parse_fundamentals_revenue_growth(self):
        html = '<span>Revenue growth</span> <span class="val">-5.3 %</span>'
        result = ScreenerScraper._parse_fundamentals(html, "TEST")
        assert result is not None
        assert result["quarterly_revenue_growth_pct"] == -5.3

    async def test_fetch_fundamentals_404(self):
        scraper = ScreenerScraper()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        scraper._session = mock_session
        scraper._owns_session = False

        result = await scraper.fetch_fundamentals("NONEXIST")
        assert result is None

    async def test_fetch_fundamentals_success(self):
        scraper = ScreenerScraper()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="""
            <span>Stock P/E</span> <span>30.5</span>
            <span>Debt to equity</span> <span>0.8</span>
        """)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        scraper._session = mock_session
        scraper._owns_session = False

        result = await scraper.fetch_fundamentals("RELIANCE")
        assert result is not None
        assert result["pe_ratio"] == 30.5

    async def test_fetch_batch_rate_limited(self):
        scraper = ScreenerScraper()
        # Mock fetch_fundamentals to track calls
        call_count = 0

        async def mock_fetch(symbol):
            nonlocal call_count
            call_count += 1
            return {"pe_ratio": 20.0}

        scraper.fetch_fundamentals = mock_fetch

        import yolovest.data.screener as screener_mod
        original_delay = screener_mod._RATE_LIMIT_DELAY
        screener_mod._RATE_LIMIT_DELAY = 0  # Speed up test
        try:
            results = await scraper.fetch_batch(["A", "B", "C"])
            assert len(results) == 3
            assert call_count == 3
        finally:
            screener_mod._RATE_LIMIT_DELAY = original_delay


# -----------------------------------------------------------------------
# Trendlyne Tests
# -----------------------------------------------------------------------


class TestTrendlyneScraper:
    def test_parse_html_momentum(self):
        html = '<div>Trendlyne Momentum Score 7.5 / 10</div>'
        result = TrendlyneScraper._parse_html(html, "RELIANCE")
        assert result is not None
        assert result["momentum_score"] == 7.5

    def test_parse_html_volume_breakout(self):
        html = '<div>Volume Breakout detected today</div>'
        result = TrendlyneScraper._parse_html(html, "TCS")
        assert result is not None
        assert result["volume_breakout"] is True

    def test_parse_html_dma_signals(self):
        html = """
        <div>Price is above 50 day DMA</div>
        <div>Price is below 200 day DMA</div>
        """
        result = TrendlyneScraper._parse_html(html, "INFY")
        assert result is not None
        assert result["dma_50_signal"] == "above"
        assert result["dma_200_signal"] == "below"

    def test_parse_html_technical_signals(self):
        html = """
        <div>Bullish MACD crossover</div>
        <div>RSI overbought</div>
        <div>Golden Cross pattern</div>
        """
        result = TrendlyneScraper._parse_html(html, "HDFC")
        assert result is not None
        assert "macd_crossover" in result["technical_signals"]
        assert "rsi_signal" in result["technical_signals"]
        assert "golden_cross" in result["technical_signals"]

    def test_parse_html_empty(self):
        result = TrendlyneScraper._parse_html("", "UNKNOWN")
        assert result is None

    def test_parse_html_no_signals(self):
        html = "<div>No relevant technical data here</div>"
        result = TrendlyneScraper._parse_html(html, "TEST")
        assert result is None

    def test_parse_api_response_full(self):
        data = {
            "momentum_score": 8.2,
            "volume_breakout": True,
            "dma_50": "above",
            "dma_200": "above",
            "signals": ["macd_crossover", "rsi_oversold"],
        }
        result = TrendlyneScraper._parse_api_response(data, "RELIANCE")
        assert result is not None
        assert result["momentum_score"] == 8.2
        assert result["volume_breakout"] is True
        assert len(result["technical_signals"]) == 2

    def test_parse_api_response_trendlyne_score_alias(self):
        data = {"trendlyne_score": 6.0}
        result = TrendlyneScraper._parse_api_response(data, "TCS")
        assert result is not None
        assert result["momentum_score"] == 6.0

    def test_parse_api_response_empty(self):
        result = TrendlyneScraper._parse_api_response({}, "TEST")
        assert result is None

    async def test_fetch_from_api_non_json(self):
        scraper = TrendlyneScraper()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        scraper._session = mock_session
        scraper._owns_session = False

        result = await scraper._fetch_from_api("RELIANCE")
        assert result is None

    async def test_fetch_from_html_404(self):
        scraper = TrendlyneScraper()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 404
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        scraper._session = mock_session
        scraper._owns_session = False

        result = await scraper._fetch_from_html("NONEXIST")
        assert result is None

    async def test_fetch_technicals_api_fallback_to_html(self):
        scraper = TrendlyneScraper()
        scraper._fetch_from_api = AsyncMock(return_value=None)
        scraper._fetch_from_html = AsyncMock(return_value={
            "momentum_score": 5.0,
            "volume_breakout": False,
        })

        result = await scraper.fetch_technicals("RELIANCE")
        assert result is not None
        assert result["momentum_score"] == 5.0
        scraper._fetch_from_api.assert_called_once()
        scraper._fetch_from_html.assert_called_once()


# -----------------------------------------------------------------------
# Integration: Fundamentals DB round-trip
# -----------------------------------------------------------------------


class TestFundamentalsDB:
    async def test_upsert_and_query_fundamentals(self, tmp_path):
        from yolovest.data.db import Database

        db = Database(str(tmp_path / "test.db"))
        await db.initialize()

        try:
            data = {
                "pe_ratio": 25.3,
                "pb_ratio": 4.2,
                "debt_to_equity": 0.35,
                "promoter_holding_pct": 50.1,
                "quarterly_revenue_growth_pct": 12.5,
            }
            await db.upsert_fundamentals("RELIANCE", data)

            # Query via NSE universe
            await db.get_nse_universe()
            # May not have OHLCV data, so check fundamentals table directly
            cursor = await db.conn.execute(
                "SELECT * FROM fundamentals WHERE symbol = ?", ("RELIANCE",)
            )
            row = await cursor.fetchone()
            assert row is not None
            assert dict(row)["pe_ratio"] == 25.3
            assert dict(row)["debt_to_equity"] == 0.35

            # Upsert again with updated values
            data["pe_ratio"] = 26.0
            await db.upsert_fundamentals("RELIANCE", data)

            cursor = await db.conn.execute(
                "SELECT pe_ratio FROM fundamentals WHERE symbol = ?", ("RELIANCE",)
            )
            row = await cursor.fetchone()
            assert row[0] == 26.0
        finally:
            await db.close()
