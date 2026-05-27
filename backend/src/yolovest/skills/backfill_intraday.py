"""Skill: backfill-intraday — Bulk historical 5-minute OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram when the Kite paid data
plan is enabled, to give the intraday model historical depth beyond
what the heartbeat has accumulated.

Mirrors backfill-data but targets the 5-minute interval. KiteDataProvider
paginates transparently when the requested window exceeds Kite's
per-call limit for the interval.

Defaults to the **Nifty 100** universe — the liquid large-caps where
intraday MIS is actually viable, and the coherent training universe for
the intraday model (the 1-min label layer is bounded to the same set).
Pass an explicit `symbols=`, `universe="fno"` (the broader ~190-name F&O
set), or `universe="tracked"` kwarg to override.
"""

import logging

from yolovest.skills.backfill_data import BackfillDataSkill

logger = logging.getLogger(__name__)


class BackfillIntradaySkill(BackfillDataSkill):
    name = "backfill-intraday"
    description = "Bulk-fetch historical 5-minute intraday OHLCV"

    _DEFAULT_INTERVAL = "5minute"
    _DEFAULT_UNIVERSE = "nifty100"

    def _default_days(self) -> int:
        return self.ctx.config.market_data.intraday_backfill_days


class BackfillIntraday1mSkill(BackfillIntradaySkill):
    """1-minute backfill — the label-precision layer for the intraday model.

    The intraday model decides/trades on 5-min bars, but the triple-barrier
    label resolves the target-before-SL ordering *inside* each 5-min bar on
    the 1-min series. 1-min bars are by far the heaviest series in the DB
    (~375 bars/symbol/day), so this layer is deliberately bounded — both to
    keep the operational SQLite file (and its boot-time WAL checkpoint +
    backups) sane and because the 1-min data is only consumed at train time
    for label resolution, never at inference:

    - **Universe**: Nifty 100 (inherited from the 5-min layer) — the liquid
      large-caps where intraday MIS is actually viable.
    - **Window**: capped at the intraday retention horizon
      (``retention.intraday_ohlcv_days``, ~365d) even when the 5-min backfill
      depth (``intraday_backfill_days``) is set deeper — there's no point
      holding 1-min bars the retention sweep would never let us keep anyway.

    1-min is paged at 60 days/request by Kite, so even bounded this is a long
    run. Prefer running it after-hours / on a weekend so it doesn't starve
    the heartbeat's ingest of the Kite rate budget.
    """

    name = "backfill-intraday-1m"
    description = "Bulk-fetch historical 1-minute intraday OHLCV (label-precision layer)"

    _DEFAULT_INTERVAL = "1m"

    def _default_days(self) -> int:
        md = self.ctx.config.market_data
        return min(
            md.intraday_backfill_days,
            self.ctx.config.database.retention.intraday_ohlcv_days,
        )
