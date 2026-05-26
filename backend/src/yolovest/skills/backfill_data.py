"""Skill: backfill-data — Bulk historical OHLCV backfill.

Trigger: MANUAL — run via dashboard or Telegram to seed historical data.

Fetches N days of daily OHLCV for every symbol the system currently tracks
(watchlist + user_watchlist + market-regime index), using the active provider
chain. Upserts into DB with deduplication, so it's safe to run repeatedly.

Typical use:
- Bootstrapping a fresh install so model-retrain has enough bars.
- Refreshing all history after upgrading data providers (e.g. switching from
  free providers to a paid Kite plan) to eliminate adjustment/source drift.
"""

import asyncio
import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger

logger = logging.getLogger(__name__)


class BackfillDataSkill(SkillBase):
    name = "backfill-data"
    description = "Bulk-fetch historical OHLCV data for model training"
    trigger = SkillTrigger.MANUAL
    schedule = None

    # Subclasses (e.g. BackfillIntradaySkill) override these to switch interval.
    _DEFAULT_INTERVAL = "daily"

    # Default symbol universe when neither `symbols` nor `universe` is passed.
    # "tracked" = watchlist + user_watchlist + regime index.
    # "fno"     = the live F&O equity underlyings (intraday-model universe).
    _DEFAULT_UNIVERSE = "tracked"

    # Pacing is now handled centrally by KiteRateLimiter (general 10 req/s)
    # plus KiteDataProvider._throttle_historical (tighter 2.5 req/s on the
    # historical_data endpoint). The per-symbol skill-level sleep this used
    # to do is redundant and only slowed things down.
    _PER_SYMBOL_DELAY_SEC = 0.0

    def _default_days(self) -> int:
        """Default lookback in days. Subclasses can override."""
        return self.ctx.config.market_data.backfill_days

    def should_run(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> SkillResult:
        interval = kwargs.get("interval", self._DEFAULT_INTERVAL)
        days = int(kwargs.get("days", self._default_days()))

        symbols = kwargs.get("symbols")
        if symbols is None:
            universe = kwargs.get("universe", self._DEFAULT_UNIVERSE)
            symbols = await self._collect_symbols(universe)

        results: dict[str, Any] = {
            "interval": interval,
            "days_requested": days,
            "symbols_total": len(symbols),
            "symbols_processed": 0,
            "total_bars_stored": 0,
            "errors": [],
            "newly_quarantined": [],
        }

        if not symbols:
            logger.warning("%s: no symbols to process", self.name)
            return SkillResult(success=True, skill_name=self.name, data=results)

        logger.info(
            "%s: starting — %d symbols × %d days × %s",
            self.name, len(symbols), days, interval,
        )

        source_label = "backfill" if interval == "daily" else f"backfill_{interval}"

        for idx, symbol in enumerate(symbols, 1):
            try:
                bars = await self.ctx.market_data.get_ohlcv(
                    symbol, interval, days=days, skip_stale_check=True,
                )
                if bars:
                    count = await self.ctx.db.upsert_ohlcv(
                        symbol, interval, bars,
                        self._ingest_source(symbol, source_label),
                    )
                    results["total_bars_stored"] += count
                    try:
                        await self.ctx.db.record_fetch_success(symbol)
                    except Exception:
                        logger.debug("record_fetch_success failed", exc_info=True)
                else:
                    logger.warning("%s: no data returned for %s", self.name, symbol)
                    await self._record_failure(symbol, "no data returned", results)
                results["symbols_processed"] += 1
            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")
                logger.warning("%s: failed for %s: %s", self.name, symbol, e)
                await self._record_failure(symbol, str(e), results)

            # Progress log every 25 symbols so long runs are visible
            if idx % 25 == 0 or idx == len(symbols):
                logger.info(
                    "%s: progress %d/%d — bars_stored=%d, errors=%d",
                    self.name, idx, len(symbols),
                    results["total_bars_stored"], len(results["errors"]),
                )

            # Pace requests so a concurrent heartbeat can still get through
            if idx < len(symbols):
                await asyncio.sleep(self._PER_SYMBOL_DELAY_SEC)

        all_failed = results["symbols_processed"] == 0 and len(results["errors"]) > 0
        return SkillResult(
            success=not all_failed,
            skill_name=self.name,
            data=results,
        )

    async def _record_failure(
        self, symbol: str, reason: str, results: dict[str, Any],
    ) -> None:
        """Bump the per-symbol failure counter; surface auto-quarantine."""
        try:
            now_quarantined = await self.ctx.db.record_fetch_failure(symbol, reason)
        except Exception:
            logger.debug("record_fetch_failure failed", exc_info=True)
            return
        if now_quarantined:
            results["newly_quarantined"].append(symbol)
            logger.warning(
                "%s: %s auto-quarantined after repeated fetch failures (last: %s). "
                "Configure a replacement on the Quarantine page or it will be "
                "skipped on the next run.",
                self.name, symbol, reason,
            )

    async def _collect_symbols(self, universe: str) -> list[str]:
        """Resolve the symbol set for the requested universe."""
        if universe == "fno":
            return await self._collect_fno_symbols()
        return await self._collect_tracked_symbols()

    async def _collect_fno_symbols(self) -> list[str]:
        """F&O equity underlyings — the intraday-model universe.

        Prefers a live NFO instrument-master fetch (authoritative, current);
        falls back to whatever ingest-fno has accumulated in fno_daily, then
        to the tracked set if neither is available.
        """
        from yolovest.data.fno_provider import fetch_fno_underlyings

        names: list[str] = []
        kite = getattr(self.ctx.broker, "_kite", None)
        token = getattr(self.ctx.broker, "_access_token", None)
        if kite is not None and token and token != "paper_token":
            try:
                names = await fetch_fno_underlyings(kite)
            except Exception:
                logger.warning(
                    "%s: live F&O underlying fetch failed", self.name, exc_info=True,
                )
        if not names:
            try:
                names = await self.ctx.db.get_distinct_fno_underlyings()
            except Exception:
                logger.debug("%s: fno_daily lookup failed", self.name, exc_info=True)
        if not names:
            logger.warning(
                "%s: no F&O underlyings resolved (authenticate Kite or run "
                "ingest-fno first) — falling back to tracked symbols",
                self.name,
            )
            return await self._collect_tracked_symbols()
        return await self.ctx.db.resolve_symbols_with_replacements(sorted(set(names)))

    async def _collect_tracked_symbols(self) -> list[str]:
        """Default symbol set: every stock the system currently tracks.

        Composed of: market-scan watchlist + user-pinned watchlist + the
        market-regime index. Falls back to scanning.seed_symbols only if
        nothing is tracked yet (truly fresh install).
        """
        symbols: set[str] = set()
        try:
            for row in await self.ctx.db.get_watchlist():
                if sym := row.get("symbol"):
                    symbols.add(sym)
        except Exception:
            logger.debug("backfill-data: could not read watchlist", exc_info=True)
        try:
            for row in await self.ctx.db.get_user_watchlist():
                if sym := row.get("symbol"):
                    symbols.add(sym)
        except Exception:
            logger.debug("backfill-data: could not read user_watchlist", exc_info=True)

        regime = self.ctx.config.strategy.market_regime
        if regime.enabled and regime.index_symbol:
            symbols.add(regime.index_symbol)

        if not symbols:
            logger.info(
                "backfill-data: no tracked symbols found, falling back to seed_symbols",
            )
            base = list(self.ctx.config.scanning.seed_symbols)
        else:
            base = sorted(symbols)

        # Apply user-configured quarantine replacements (ZOMATO -> ETERNAL, etc.)
        return await self.ctx.db.resolve_symbols_with_replacements(base)
