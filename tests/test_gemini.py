"""Tests for GeminiLLM — all 7 methods with mocked API."""

import json
from unittest.mock import MagicMock

import pytest

from yolovest.llm.gemini import GeminiLLM
from yolovest.models.schemas import (
    PortfolioState,
    Signal,
    TradeContext,
)


@pytest.fixture
def gemini():
    """GeminiLLM with mocked client."""
    llm = GeminiLLM(api_key="test-key")
    return llm


def _mock_response(text: str):
    """Create a mock Gemini API response."""
    response = MagicMock()
    response.text = text
    return response


def _patch_generate(gemini, response_text: str):
    """Patch the _generate method to return canned text."""
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _mock_response(response_text)
    gemini._client = mock_client
    return mock_client


class TestPing:
    async def test_ping_success(self, gemini):
        _patch_generate(gemini, '{"status": "ok"}')
        assert await gemini.ping() is True

    async def test_ping_failure(self, gemini):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ConnectionError("down")
        gemini._client = mock_client
        assert await gemini.ping() is False


class TestReviewTrade:
    async def test_review_approve(self, gemini):
        _patch_generate(gemini, json.dumps({
            "decision": "APPROVE",
            "reasoning": "Strong signal with good risk-reward",
            "adjusted_size": None,
        }))
        context = TradeContext(
            signal=Signal(
                symbol="RELIANCE", signal_type="BUY", entry_price=2500,
                target_price=2600, stop_loss_price=2450, position_size=10,
                expected_holding_period="intraday", confidence_score=0.8,
                model_version="v1",
            ),
            portfolio=PortfolioState(total_capital=100000, available_cash=80000, exposure_pct=0.2),
        )
        result = await gemini.review_trade(context)
        assert result.decision == "APPROVE"
        assert result.reasoning != ""

    async def test_review_resize(self, gemini):
        _patch_generate(gemini, json.dumps({
            "decision": "RESIZE",
            "reasoning": "Too much exposure, reduce size",
            "adjusted_size": 5,
        }))
        context = TradeContext(
            signal=Signal(
                symbol="TCS", signal_type="BUY", entry_price=3500,
                target_price=3600, stop_loss_price=3400, position_size=20,
                expected_holding_period="3d", confidence_score=0.7,
                model_version="v1",
            ),
            portfolio=PortfolioState(total_capital=100000, available_cash=50000, exposure_pct=0.5),
        )
        result = await gemini.review_trade(context)
        assert result.decision == "RESIZE"
        assert result.adjusted_size == 5


class TestAnalyzeSentiment:
    async def test_bullish_sentiment(self, gemini):
        _patch_generate(gemini, json.dumps({
            "symbol": "RELIANCE",
            "sentiment": "bullish",
            "confidence": 0.85,
            "key_drivers": ["Strong quarterly results", "New energy investments"],
        }))
        result = await gemini.analyze_sentiment(
            "RELIANCE", ["Reliance Q3 profit up 15%", "New solar plant announced"]
        )
        assert result.symbol == "RELIANCE"
        assert result.sentiment == "bullish"
        assert result.confidence == 0.85
        assert len(result.key_drivers) == 2


class TestSummarizeWithWebGrounding:
    async def test_web_grounding(self, gemini):
        _patch_generate(gemini, json.dumps({
            "query": "Indian market overnight developments",
            "summary": "US markets closed higher. GIFT Nifty indicates positive open.",
            "sources": ["https://example.com/news1"],
        }))
        result = await gemini.summarize_with_web_grounding(
            "Summarize overnight market developments affecting Indian markets"
        )
        assert "GIFT Nifty" in result.summary
        assert len(result.sources) >= 1


class TestValidateWatchlist:
    async def test_validate(self, gemini):
        _patch_generate(gemini, json.dumps({
            "approved_symbols": ["RELIANCE", "TCS"],
            "rejected_symbols": ["YESBANK"],
            "reasoning": {"YESBANK": "High debt, governance concerns"},
            "market_narrative": "Broad market rally led by large caps",
        }))
        result = await gemini.validate_watchlist(
            shortlist=[{"symbol": "RELIANCE"}, {"symbol": "TCS"}, {"symbol": "YESBANK"}],
            sector_analysis={"IT": "strong"},
            premarket_context={"gift_nifty_change_pct": 0.5},
        )
        assert "RELIANCE" in result.approved_symbols
        assert "YESBANK" in result.rejected_symbols


class TestSummarizeMarketDay:
    async def test_market_summary(self, gemini):
        _patch_generate(gemini, json.dumps({
            "date": "2026-03-22",
            "market_sentiment": "bullish",
            "key_events": ["RBI policy unchanged", "IT sector rally"],
            "sector_highlights": {"IT": "Up 2.5%", "Banks": "Flat"},
            "outlook": "Positive momentum expected to continue",
        }))
        result = await gemini.summarize_market_day()
        assert result.market_sentiment == "bullish"
        assert len(result.key_events) >= 1


class TestAnalyzePredictionFailures:
    async def test_failure_analysis(self, gemini):
        _patch_generate(gemini, json.dumps({
            "patterns_identified": ["Overconfident on momentum stocks"],
            "common_failure_modes": ["Gap-down openings not handled"],
            "recommendations": ["Add overnight risk factor"],
            "summary": "Model struggles with gap scenarios",
        }))
        result = await gemini.analyze_prediction_failures(
            [{"symbol": "TATAMOTORS", "predicted": "BUY", "actual_pnl": -3.5}]
        )
        assert len(result.patterns_identified) >= 1
        assert len(result.recommendations) >= 1


class TestJSONParseFailure:
    async def test_invalid_json_retries_and_succeeds(self, gemini):
        """Test that invalid JSON triggers a retry with corrective prompt."""
        mock_client = MagicMock()
        # First call returns invalid JSON, second returns valid JSON
        mock_client.models.generate_content.side_effect = [
            _mock_response("This is not JSON at all"),
            _mock_response('{"symbol": "RELIANCE", "sentiment": "bullish", '
                          '"confidence": 0.8, "key_drivers": ["strong earnings"]}'),
        ]
        gemini._client = mock_client
        gemini._retry_base_delay = 0.01

        result = await gemini.analyze_sentiment("RELIANCE", ["Good Q3 results"])
        assert result.sentiment == "bullish"
        # _generate was called twice (once invalid, once valid)
        assert mock_client.models.generate_content.call_count == 2

    async def test_invalid_json_all_retries_exhausted(self, gemini):
        """Test that persistent invalid JSON raises JSONDecodeError."""
        mock_client = MagicMock()
        # All calls return invalid JSON
        mock_client.models.generate_content.return_value = _mock_response(
            "I cannot provide JSON output"
        )
        gemini._client = mock_client
        gemini._retry_base_delay = 0.01
        gemini._max_retries = 2

        with pytest.raises(json.JSONDecodeError):
            await gemini.analyze_sentiment("RELIANCE", ["Bad news"])

    async def test_partial_json_retries(self, gemini):
        """Test handling of truncated/partial JSON."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = [
            _mock_response('{"symbol": "RELIANCE", "sentiment": '),  # truncated
            _mock_response('{"symbol": "RELIANCE", "sentiment": "neutral", '
                          '"confidence": 0.5, "key_drivers": []}'),
        ]
        gemini._client = mock_client
        gemini._retry_base_delay = 0.01

        result = await gemini.analyze_sentiment("RELIANCE", ["Mixed signals"])
        assert result.sentiment == "neutral"


class TestRetryBehavior:
    async def test_retries_on_failure(self, gemini):
        mock_client = MagicMock()
        # Fail twice, succeed on third
        mock_client.models.generate_content.side_effect = [
            ConnectionError("timeout"),
            ConnectionError("timeout"),
            _mock_response('{"status": "ok"}'),
        ]
        gemini._client = mock_client
        gemini._retry_base_delay = 0.01  # fast retries for test

        result = await gemini.ping()
        assert result is True
        assert mock_client.models.generate_content.call_count == 3
