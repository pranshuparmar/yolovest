"""Skill: backfill-intraday — Bulk historical 5-minute OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram when the Kite paid data
plan is enabled, to give the intraday model historical depth beyond
what the heartbeat has accumulated.

Mirrors backfill-data but targets the 5-minute interval. KiteDataProvider
paginates transparently when the requested window exceeds Kite's
per-call limit for the interval.

Defaults to the **F&O equity universe** (~190 names) rather than the daily
watchlist — that's the intraday model's tradable universe, and the only
set worth building deep 5-min history for. Pass an explicit `symbols=` or
`universe="tracked"` kwarg to override.
"""

import logging

from yolovest.skills.backfill_data import BackfillDataSkill

logger = logging.getLogger(__name__)


class BackfillIntradaySkill(BackfillDataSkill):
    name = "backfill-intraday"
    description = "Bulk-fetch historical 5-minute intraday OHLCV"

    _DEFAULT_INTERVAL = "5minute"
    _DEFAULT_UNIVERSE = "fno"

    def _default_days(self) -> int:
        return self.ctx.config.market_data.intraday_backfill_days


class BackfillIntraday1mSkill(BackfillIntradaySkill):
    """1-minute backfill — the label-precision layer for the intraday model.

    The intraday model decides/trades on 5-min bars, but the triple-barrier
    label resolves the target-before-SL ordering *inside* each 5-min bar on
    the 1-min series. That needs 1-min coverage over the same F&O universe
    and window as the 5-min backfill, so this runs the identical backbone
    with interval='1m'. 1-min is ~5x heavier and Kite pages it at 60
    days/request, so expect a long run.
    """

    name = "backfill-intraday-1m"
    description = "Bulk-fetch historical 1-minute intraday OHLCV (label-precision layer)"

    _DEFAULT_INTERVAL = "1m"
