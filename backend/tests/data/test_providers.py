"""Tests for individual market data providers with mocked HTTP/data."""

import importlib
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

pd = pytest.importorskip("pandas", reason="pandas required for provider tests")


def _has_module(name: str) -> bool:
    """Check if a module is importable without actually importing it."""
    return importlib.util.find_spec(name) is not None


@pytest.mark.skipif(
    not _has_module("jugaad_data"),
    reason="jugaad_data not installed",
)
class TestJugaadDataProvider:
    """Test JugaadDataProvider data transformation."""

    async def test_fetch_stock_data_transforms_df(self):
        """Test that DataFrame rows are correctly transformed to OHLCVBar."""
        from yolovest.data.jugaad import JugaadDataProvider

        mock_df = pd.DataFrame({
            "DATE": [pd.Timestamp("2026-03-20"), pd.Timestamp("2026-03-21")],
            "OPEN": [2500.0, 2510.0],
            "HIGH": [2550.0, 2560.0],
            "LOW": [2480.0, 2490.0],
            "CLOSE": [2530.0, 2540.0],
            "VOLUME": [1000000, 1200000],
        })

        with patch("jugaad_data.nse.stock_df", return_value=mock_df):
            bars = JugaadDataProvider._fetch_stock_data(
                "RELIANCE",
                datetime(2026, 3, 20).date(),
                datetime(2026, 3, 21).date(),
            )

        assert len(bars) == 2
        assert bars[0].open == 2500.0
        assert bars[1].close == 2540.0
        assert bars[0].volume == 1000000
        # Sorted ascending
        assert bars[0].timestamp < bars[1].timestamp

    async def test_fetch_uses_tottrdqty_fallback(self):
        """Test that TOTTRDQTY is used when VOLUME column is missing."""
        from yolovest.data.jugaad import JugaadDataProvider

        mock_df = pd.DataFrame({
            "DATE": [pd.Timestamp("2026-03-20")],
            "OPEN": [2500.0],
            "HIGH": [2550.0],
            "LOW": [2480.0],
            "CLOSE": [2530.0],
            "TOTTRDQTY": [999999],
        })

        with patch("jugaad_data.nse.stock_df", return_value=mock_df):
            bars = JugaadDataProvider._fetch_stock_data(
                "RELIANCE",
                datetime(2026, 3, 20).date(),
                datetime(2026, 3, 20).date(),
            )

        assert len(bars) == 1
        assert bars[0].volume == 999999

    async def test_daily_only(self):
        """JugaadDataProvider rejects non-daily intervals."""
        from yolovest.data.jugaad import JugaadDataProvider

        provider = JugaadDataProvider()
        with pytest.raises(ValueError, match="only supports daily"):
            await provider.get_ohlcv("RELIANCE", "5minute", 30)


@pytest.mark.skipif(
    not _has_module("yfinance"),
    reason="yfinance not installed",
)
class TestYFinanceProvider:
    """Test YFinanceProvider data transformation."""

    async def test_fetch_data_transforms_df(self):
        """Test that yfinance DataFrame is correctly transformed."""
        from yolovest.data.yfinance_provider import YFinanceProvider

        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-03-20 09:15:00+05:30"),
            pd.Timestamp("2026-03-21 09:15:00+05:30"),
        ])
        mock_df = pd.DataFrame({
            "Open": [2500.0, 2510.0],
            "High": [2550.0, 2560.0],
            "Low": [2480.0, 2490.0],
            "Close": [2530.0, 2540.0],
            "Volume": [1000000, 1200000],
        }, index=idx)

        mock_ticker = MagicMock()
        mock_ticker.history.return_value = mock_df

        provider = YFinanceProvider()

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.return_value = mock_ticker
            bars = provider._fetch_data("RELIANCE", "1d", 30)

        assert len(bars) == 2
        assert bars[0].open == 2500.0
        # Timezone should be stripped
        assert bars[0].timestamp.tzinfo is None

    async def test_nse_suffix_added(self):
        """Test that .NS suffix is added for NSE symbols."""
        from yolovest.data.yfinance_provider import YFinanceProvider

        provider = YFinanceProvider()
        assert provider._nse_symbol("RELIANCE") == "RELIANCE.NS"
        assert provider._nse_symbol("RELIANCE.NS") == "RELIANCE.NS"

    async def test_unsupported_interval_raises(self):
        """Test that unsupported intervals raise ValueError."""
        from yolovest.data.yfinance_provider import YFinanceProvider

        provider = YFinanceProvider()
        with pytest.raises(ValueError, match="Unsupported interval"):
            await provider.get_ohlcv("RELIANCE", "1h", 30)


@pytest.mark.skipif(
    not _has_module("tvDatafeed"),
    reason="tvDatafeed not installed",
)
class TestTVDatafeedProvider:
    """Test TVDatafeedProvider data transformation."""

    async def test_fetch_data_transforms_df(self):
        """Test that tvDatafeed DataFrame is correctly transformed."""
        from yolovest.data.tvfeed import TVDatafeedProvider

        idx = pd.DatetimeIndex([
            pd.Timestamp("2026-03-20 09:15:00"),
            pd.Timestamp("2026-03-20 09:20:00"),
        ])
        mock_df = pd.DataFrame({
            "open": [2500.0, 2505.0],
            "high": [2510.0, 2515.0],
            "low": [2495.0, 2500.0],
            "close": [2505.0, 2510.0],
            "volume": [50000, 60000],
        }, index=idx)

        mock_tv = MagicMock()
        mock_tv.get_hist.return_value = mock_df

        provider = TVDatafeedProvider()
        provider._tv = mock_tv

        with patch("yolovest.data.tvfeed.Interval", create=True) as mock_interval:
            mock_interval.in_5_minute = "in_5_minute"
            bars = provider._fetch_data("RELIANCE", "in_5_minute", 75)

        assert len(bars) == 2
        assert bars[0].open == 2500.0
        assert bars[1].volume == 60000

    async def test_empty_df_returns_empty(self):
        """Test that empty DataFrame returns empty list."""
        from yolovest.data.tvfeed import TVDatafeedProvider

        mock_tv = MagicMock()
        mock_tv.get_hist.return_value = pd.DataFrame()

        provider = TVDatafeedProvider()
        provider._tv = mock_tv

        with patch("yolovest.data.tvfeed.Interval", create=True) as mock_interval:
            mock_interval.in_5_minute = "in_5_minute"
            bars = provider._fetch_data("RELIANCE", "in_5_minute", 75)

        assert bars == []

    async def test_unsupported_interval_raises(self):
        """Test that unsupported intervals raise ValueError."""
        from yolovest.data.tvfeed import TVDatafeedProvider

        provider = TVDatafeedProvider()
        with pytest.raises(ValueError, match="Unsupported interval"):
            await provider.get_ohlcv("RELIANCE", "1h", 15)

    async def test_days_capped_at_15(self):
        """Test that days parameter is capped at 15 for free tier."""
        from yolovest.data.tvfeed import TVDatafeedProvider

        mock_tv = MagicMock()
        mock_tv.get_hist.return_value = pd.DataFrame()

        provider = TVDatafeedProvider()
        provider._tv = mock_tv

        with patch("yolovest.data.tvfeed.Interval", create=True) as mock_interval:
            mock_interval.in_5_minute = "in_5_minute"
            await provider.get_ohlcv("RELIANCE", "5minute", days=30)

        # n_bars should be 15 * 75 = 1125 (capped at 15 days)
        call_args = mock_tv.get_hist.call_args
        assert call_args.kwargs.get("n_bars", call_args[1].get("n_bars")) == 1125
