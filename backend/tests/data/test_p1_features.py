"""Tests for P1 features: Google Finance, failure analysis wiring,
LLM review accuracy, slippage feedback, and news aggregator wiring."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Google Finance Scraper
# ---------------------------------------------------------------------------


class TestGoogleFinanceScraper:
    async def test_fetch_market_indices_success(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            '<div>.NSEI data-value="22500.50"</div>'
            '<div>.BSESN data-value="73200.10"</div>'
        )
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        indices = await scraper.fetch_market_indices()

        assert "NIFTY_50" in indices
        assert indices["NIFTY_50"]["value"] == 22500.50

    async def test_fetch_market_indices_failure(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("network error"))
        scraper._session = mock_client
        scraper._owns_session = False

        indices = await scraper.fetch_market_indices()
        assert indices == {}

    async def test_fetch_trending_tickers(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            '/quote/RELIANCE:NSE" '
            '/quote/TCS:NSE" '
            '/quote/INFY:NSE" '
        )
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        tickers = await scraper.fetch_trending_tickers()

        assert len(tickers) >= 3
        symbols = [t["symbol"] for t in tickers]
        assert "RELIANCE" in symbols
        assert "TCS" in symbols

    async def test_fetch_market_news(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper(rate_limit_delay=0)
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            '<div class="News-article">'
            '<a>RELIANCE reports strong quarterly earnings growth</a></div>'
        )
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        news = await scraper.fetch_market_news(["RELIANCE"])

        assert len(news) >= 1
        assert news[0].source == "google_finance"
        assert "RELIANCE" in news[0].symbols

    async def test_fetch_all(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper(rate_limit_delay=0)
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>empty</html>"
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        result = await scraper.fetch_all(["RELIANCE"])

        assert "indices" in result
        assert "trending_tickers" in result
        assert "news" in result

    async def test_health_check_success(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>content</html>"
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        assert await scraper.health_check()

    async def test_health_check_failure(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("down"))
        scraper._session = mock_client
        scraper._owns_session = False

        assert not await scraper.health_check()

    async def test_close_session(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        scraper._session = mock_client
        scraper._owns_session = True

        await scraper.close()
        mock_client.aclose.assert_awaited_once()
        assert scraper._session is None

    async def test_close_external_session_not_closed(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        scraper._session = mock_client
        scraper._owns_session = False

        await scraper.close()
        mock_client.aclose.assert_not_awaited()

    async def test_non_200_returns_none(self):
        from yolovest.data.google_finance import GoogleFinanceScraper

        scraper = GoogleFinanceScraper()
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_client.get = AsyncMock(return_value=mock_resp)
        scraper._session = mock_client
        scraper._owns_session = False

        result = await scraper._fetch_page("https://google.com/finance")
        assert result is None


# ---------------------------------------------------------------------------
# Failure Analysis Wiring (predict-track)
# ---------------------------------------------------------------------------


class TestFailureAnalysisWiring:
    async def test_failure_analysis_triggers_on_enough_failures(self, app_context):
        from yolovest.skills.predict_track import PredictTrackSkill

        skill = PredictTrackSkill(app_context)

        # Set up 6 predictions that will all fail (direction wrong)
        pending = [
            {"id": f"P-{i}", "symbol": "RELIANCE", "entry_price": 2500,
             "predicted_direction": "BUY", "predicted_target": 2600,
             "created_at": "2026-03-01T10:00:00",
             "prediction_end_time": "2026-03-02T15:30:00"}
            for i in range(6)
        ]
        skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        # End-date window close is below entry → all predictions wrong
        skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(2410.0, 2415.0, 2390.0, 2400.0, "2026-03-02")]
        )
        skill.ctx.db.get_prediction_outcomes = AsyncMock(return_value=[
            {"direction_correct": False} for _ in range(6)
        ])

        result = await skill.execute(mode="score")

        assert result.success
        assert result.data["predictions_scored"] == 6
        assert result.data["correct"] == 0
        assert result.data["failure_analysis_triggered"]
        skill.ctx.llm.analyze_prediction_failures.assert_awaited_once()
        skill.ctx.db.store_failure_analysis.assert_awaited_once()

    async def test_failure_analysis_skipped_when_few_failures(self, app_context):
        from yolovest.skills.predict_track import PredictTrackSkill

        skill = PredictTrackSkill(app_context)

        # Only 2 failures — below threshold of 5
        pending = [
            {"id": f"P-{i}", "symbol": "TCS", "entry_price": 3500,
             "predicted_direction": "BUY", "predicted_target": 3600,
             "created_at": "2026-03-01T10:00:00",
             "prediction_end_time": "2026-03-02T15:30:00"}
            for i in range(2)
        ]
        skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(3410.0, 3420.0, 3390.0, 3400.0, "2026-03-02")]
        )

        result = await skill.execute(mode="score")

        assert result.data["predictions_scored"] == 2
        assert not result.data["failure_analysis_triggered"]

    async def test_failure_analysis_handles_llm_error(self, app_context):
        from yolovest.skills.predict_track import PredictTrackSkill

        skill = PredictTrackSkill(app_context)

        pending = [
            {"id": f"P-{i}", "symbol": "INFY", "entry_price": 1800,
             "predicted_direction": "BUY", "predicted_target": 1900,
             "created_at": "2026-03-01T10:00:00",
             "prediction_end_time": "2026-03-02T15:30:00"}
            for i in range(6)
        ]
        skill.ctx.db.get_unscored_predictions = AsyncMock(return_value=pending)
        skill.ctx.db.get_daily_ohlc_between = AsyncMock(
            return_value=[(1710.0, 1715.0, 1690.0, 1700.0, "2026-03-02")]
        )
        skill.ctx.db.get_prediction_outcomes = AsyncMock(return_value=[
            {"direction_correct": False} for _ in range(6)
        ])
        skill.ctx.llm.analyze_prediction_failures = AsyncMock(
            side_effect=Exception("LLM down")
        )

        result = await skill.execute(mode="score")

        assert result.success
        assert not result.data["failure_analysis_triggered"]


# ---------------------------------------------------------------------------
# Slippage Feedback Loop
# ---------------------------------------------------------------------------


class TestSlippageFeedback:
    async def test_slippage_penalty_applied(self, app_context):
        from yolovest.skills.risk_check import RiskCheckSkill

        skill = RiskCheckSkill(app_context)

        # Set high slippage for RELIANCE
        skill.ctx.db.get_slippage_stats = AsyncMock(return_value={
            "total_trades": 10,
            "avg_slippage": 15.0,
            "max_slippage": 30.0,
            "avg_slippage_pct": 0.006,  # 0.6% — above 0.2% threshold
            "by_symbol": {"RELIANCE": {"count": 10, "avg_slippage": 15}},
        })

        signal = {
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500,
            "stop_loss_price": 2450,
            "target_price": 2600,
            "position_size": 10,
        }

        # Make market hours and order window pass
        with patch.object(skill.ctx.market_hours, "is_order_window", return_value=True):
            result = await skill.execute(signal=signal)

        assert result.success
        assert result.data["approved"]
        assert result.data["slippage_penalty"] > 0
        # Size should be reduced
        assert result.data["adjusted_size"] < 40  # base would be ~40

    async def test_no_slippage_penalty_for_new_symbol(self, app_context):
        from yolovest.skills.risk_check import RiskCheckSkill

        skill = RiskCheckSkill(app_context)

        # No historical trades
        skill.ctx.db.get_slippage_stats = AsyncMock(return_value={
            "total_trades": 0,
            "avg_slippage": 0,
            "max_slippage": 0,
            "avg_slippage_pct": 0,
            "by_symbol": {},
        })

        signal = {
            "symbol": "NEWSTOCK",
            "signal_type": "BUY",
            "entry_price": 2500,
            "stop_loss_price": 2450,
            "target_price": 2600,
            "position_size": 10,
        }

        with patch.object(skill.ctx.market_hours, "is_order_window", return_value=True):
            result = await skill.execute(signal=signal)

        assert result.success
        assert result.data["approved"]
        assert result.data.get("slippage_penalty", 0) == 0

    async def test_slippage_penalty_capped_at_30pct(self, app_context):
        from yolovest.skills.risk_check import RiskCheckSkill

        skill = RiskCheckSkill(app_context)

        # Extreme slippage
        skill.ctx.db.get_slippage_stats = AsyncMock(return_value={
            "total_trades": 20,
            "avg_slippage": 50.0,
            "max_slippage": 100.0,
            "avg_slippage_pct": 0.05,  # 5% — very high
            "by_symbol": {},
        })

        penalty = await skill._get_slippage_penalty("BADSTOCK")
        assert penalty == pytest.approx(0.30)  # capped at 30%


# ---------------------------------------------------------------------------
# LLM Review Accuracy Tracking
# ---------------------------------------------------------------------------


class TestLLMReviewAccuracy:
    async def test_weekly_report_includes_llm_accuracy(self, app_context):
        from yolovest.skills.report_generate import ReportGenerateSkill

        skill = ReportGenerateSkill(app_context)

        skill.ctx.db.get_weekly_trades = AsyncMock(return_value=[
            {"symbol": "RELIANCE", "pnl": 500},
            {"symbol": "TCS", "pnl": -200},
        ])
        skill.ctx.db.get_weekly_predictions = AsyncMock(return_value=[])
        skill.ctx.db.get_weekly_llm_reviews = AsyncMock(return_value=[
            {"decision": "APPROVE", "trade_pnl": 500},
            {"decision": "APPROVE", "trade_pnl": -200},
            {"decision": "REJECT", "trade_pnl": None},
        ])
        skill.ctx.db.get_llm_review_accuracy = AsyncMock(return_value={
            "total_reviews": 3,
            "approved_count": 2,
            "rejected_count": 1,
            "approved_with_outcomes": 2,
            "profitable_approvals": 1,
            "losing_approvals": 1,
            "approval_accuracy": 0.5,
            "approved_total_pnl": 300,
            "approved_avg_pnl": 150,
        })

        result = await skill.execute(type="weekly")

        assert result.success
        assert "llm_review_accuracy" in result.data
        acc = result.data["llm_review_accuracy"]
        assert acc["approval_accuracy"] == 0.5
        assert acc["approved_count"] == 2

    async def test_weekly_report_handles_missing_accuracy(self, app_context):
        from yolovest.skills.report_generate import ReportGenerateSkill

        skill = ReportGenerateSkill(app_context)
        skill.ctx.db.get_llm_review_accuracy = AsyncMock(
            side_effect=Exception("DB error")
        )

        result = await skill.execute(type="weekly")

        assert result.success
        assert result.data["llm_review_accuracy"] is None

    async def test_weekly_report_includes_slippage_stats(self, app_context):
        from yolovest.skills.report_generate import ReportGenerateSkill

        skill = ReportGenerateSkill(app_context)
        skill.ctx.db.get_slippage_stats = AsyncMock(return_value={
            "total_trades": 5,
            "avg_slippage": 2.5,
            "max_slippage": 5.0,
            "avg_slippage_pct": 0.001,
            "by_symbol": {},
        })

        result = await skill.execute(type="weekly")

        assert result.success
        assert "slippage_stats" in result.data
        assert result.data["slippage_stats"]["total_trades"] == 5

    def test_weekly_format_includes_llm_accuracy(self):
        from yolovest.skills.report_generate import ReportGenerateSkill

        report = {
            "total_trades": 10,
            "total_pnl": 1000,
            "win_rate": 0.6,
            "llm_approvals": 8,
            "llm_rejections": 2,
            "llm_approved_pnl": 800,
            "llm_review_accuracy": {
                "approval_accuracy": 0.75,
                "profitable_approvals": 6,
                "approved_with_outcomes": 8,
            },
            "slippage_stats": {
                "total_trades": 10,
                "avg_slippage": 1.5,
                "avg_slippage_pct": 0.001,
            },
        }
        formatted = ReportGenerateSkill._format_weekly_report(report)

        assert "LLM Approval Accuracy: 75%" in formatted
        assert "Avg Slippage" in formatted


# ---------------------------------------------------------------------------
# News Aggregator Wiring
# ---------------------------------------------------------------------------


class TestNewsAggregatorWiring:
    def test_build_news_aggregator(self, sample_config):
        from yolovest.main import _build_news_aggregator
        from yolovest.news.aggregator import NewsAggregator

        sample_config.market_data.news_enabled = True
        agg = _build_news_aggregator(sample_config)
        assert isinstance(agg, NewsAggregator)
        assert len(agg.sources) == 3  # MoneyControl, ETMarkets, LiveMint

    def test_context_has_news_aggregator_field(self, app_context):
        # By default the fixture doesn't set it, so it's None
        assert hasattr(app_context, "news_aggregator")

    async def test_ingest_data_uses_context_aggregator(self, app_context):
        from yolovest.news.aggregator import NewsAggregator
        from yolovest.skills.ingest_data import IngestDataSkill

        mock_agg = AsyncMock(spec=NewsAggregator)
        mock_agg.fetch_all = AsyncMock(return_value=[])
        app_context.news_aggregator = mock_agg

        skill = IngestDataSkill(app_context)
        articles = await skill._fetch_all_news(["RELIANCE"])

        mock_agg.fetch_all.assert_awaited_once_with(["RELIANCE"])

    async def test_ingest_data_skips_when_no_aggregator(self, app_context):
        from yolovest.skills.ingest_data import IngestDataSkill

        app_context.news_aggregator = None
        skill = IngestDataSkill(app_context)
        articles = await skill._fetch_all_news(["RELIANCE"])

        assert articles == []


# ---------------------------------------------------------------------------
# Google Finance wiring in ingest-data
# ---------------------------------------------------------------------------


class TestGoogleFinanceWiring:
    async def test_ingest_data_calls_google_finance(self, app_context):
        from yolovest.skills.ingest_data import IngestDataSkill

        skill = IngestDataSkill(app_context)
        app_context.news_aggregator = None

        mock_result = {
            "indices": {"NIFTY_50": {"value": 22500}},
            "trending_tickers": [{"symbol": "RELIANCE"}],
            "news": [],
        }

        # Mock all external calls to prevent hangs
        with patch.object(skill, "_fetch_google_finance", new_callable=AsyncMock) as mock_gf, \
             patch.object(skill, "_fetch_nse_data", new_callable=AsyncMock) as mock_nse, \
             patch.object(skill, "_fetch_economic_calendar", new_callable=AsyncMock) as mock_econ, \
             patch.object(skill, "_fetch_fundamentals", new_callable=AsyncMock) as mock_fund, \
             patch.object(skill, "_fetch_technicals", new_callable=AsyncMock) as mock_tech:
            mock_gf.return_value = mock_result
            mock_nse.return_value = {}
            mock_econ.return_value = []
            mock_fund.return_value = 0
            mock_tech.return_value = 0

            result = await skill.execute(symbols=["RELIANCE"])

            assert result.success
            mock_gf.assert_awaited_once()
            assert "google_finance" in result.data

    async def test_ingest_data_handles_google_finance_failure(self, app_context):
        from yolovest.skills.ingest_data import IngestDataSkill

        skill = IngestDataSkill(app_context)
        app_context.news_aggregator = None

        with patch.object(skill, "_fetch_google_finance", new_callable=AsyncMock) as mock_gf, \
             patch.object(skill, "_fetch_nse_data", new_callable=AsyncMock) as mock_nse, \
             patch.object(skill, "_fetch_economic_calendar", new_callable=AsyncMock) as mock_econ, \
             patch.object(skill, "_fetch_fundamentals", new_callable=AsyncMock) as mock_fund, \
             patch.object(skill, "_fetch_technicals", new_callable=AsyncMock) as mock_tech:
            mock_gf.side_effect = Exception("GF down")
            mock_nse.return_value = {}
            mock_econ.return_value = []
            mock_fund.return_value = 0
            mock_tech.return_value = 0

            result = await skill.execute(symbols=["RELIANCE"])

            # Should still succeed — Google Finance failure is non-fatal
            assert result.success
