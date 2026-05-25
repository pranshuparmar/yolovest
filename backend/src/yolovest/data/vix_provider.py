"""India VIX fetcher (yfinance, EOD).

Lives outside the provider fallback chain because VIX is a NSE *index*,
not an equity — the existing providers all assume `.NS`-suffixed
equity tickers and route per-symbol failures through quarantine. VIX
gets its own thin helper so a one-off NaN doesn't quarantine "VIX" the
way it would quarantine a real stock.

Symbol on Yahoo Finance: `^INDIAVIX`.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime

from yolovest.models.schemas import OHLCVBar, is_valid_ohlc

logger = logging.getLogger(__name__)

VIX_SYMBOL = "INDIA VIX"
VIX_YF_TICKER = "^INDIAVIX"


async def fetch_vix_history(days: int = 30) -> list[OHLCVBar]:
    """Pull `days` of India VIX daily bars from yfinance.

    Returns sorted bars (oldest → newest). Empty list on any failure —
    caller decides whether to retry or skip the cycle.
    """
    return await asyncio.to_thread(_fetch_sync, days)


def _fetch_sync(days: int) -> list[OHLCVBar]:
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; VIX fetch skipped")
        return []

    from yolovest.data.yfinance_provider import configure_yfinance_cache
    configure_yfinance_cache()

    try:
        ticker = yf.Ticker(VIX_YF_TICKER)
        period = f"{days}d" if days <= 730 else "max"
        df = ticker.history(period=period, interval="1d")
    except Exception:
        logger.exception("VIX yfinance fetch failed")
        return []

    if df.empty:
        return []

    bars: list[OHLCVBar] = []
    for idx, row in df.iterrows():
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        if not is_valid_ohlc(o, h, l, c):
            continue
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else datetime.fromisoformat(str(idx))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        # VIX has no traded volume; yfinance returns 0. Keep schema happy.
        vol_raw = row.get("Volume", 0)
        try:
            volume = int(vol_raw) if not math.isnan(float(vol_raw)) else 0
        except (TypeError, ValueError):
            volume = 0
        bars.append(
            OHLCVBar(
                timestamp=ts,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=volume,
            )
        )
    bars.sort(key=lambda b: b.timestamp)
    return bars
