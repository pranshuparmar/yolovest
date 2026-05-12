"""Skill: backfill-intraday — Bulk historical 5-minute OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram once you've enabled the
Kite paid data plan, to give the intraday model real historical depth
instead of the few sessions accumulated by the heartbeat.

Mirrors backfill-data but targets the 5-minute interval over a shorter
default window (1 year of intraday is plenty; 3 years would explode DB
size with little ML benefit). Kite's per-call limit for 5-minute bars
is 100 days, so KiteDataProvider transparently paginates.

Estimated runtime for ~130 symbols: 5–10 minutes (each symbol triggers
multiple chunked Kite calls, paced to leave headroom for the heartbeat).
"""

import logging

from yolovest.skills.backfill_data import BackfillDataSkill

logger = logging.getLogger(__name__)


class BackfillIntradaySkill(BackfillDataSkill):
    name = "backfill-intraday"
    description = "Bulk-fetch historical 5-minute intraday OHLCV"

    _DEFAULT_INTERVAL = "5minute"
    # Heavier per-call than daily (multiple chunks per symbol), so be more
    # conservative about how often we hit Kite to keep heartbeat unblocked.
    _PER_SYMBOL_DELAY_SEC = 0.3

    def _default_days(self) -> int:
        # 1 year of 5-minute bars ≈ 250 trading days × 75 bars = ~19k rows/symbol.
        # Going beyond that bloats the DB without meaningfully improving the
        # intraday model (which uses recent volatility/momentum, not multi-year trends).
        return 365
