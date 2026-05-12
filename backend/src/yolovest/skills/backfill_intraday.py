"""Skill: backfill-intraday — Bulk historical 5-minute OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram when the Kite paid data
plan is enabled, to give the intraday model historical depth beyond
what the heartbeat has accumulated.

Mirrors backfill-data but targets the 5-minute interval. KiteDataProvider
paginates transparently when the requested window exceeds Kite's
per-call limit for the interval.
"""

import logging

from yolovest.skills.backfill_data import BackfillDataSkill

logger = logging.getLogger(__name__)


class BackfillIntradaySkill(BackfillDataSkill):
    name = "backfill-intraday"
    description = "Bulk-fetch historical 5-minute intraday OHLCV"

    _DEFAULT_INTERVAL = "5minute"

    def _default_days(self) -> int:
        # Lookback window for intraday history. Longer windows balloon
        # row counts (~75 bars per trading day per symbol) for marginal
        # ML benefit on short-horizon models.
        return 365
