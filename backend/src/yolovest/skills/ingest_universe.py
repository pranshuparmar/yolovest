"""Skill: ingest-universe — Populate OHLCV for the full NSE scanning universe.

Trigger: CRON (daily before market open) + MANUAL
Pipeline position: Runs before heartbeat cycle so market-scan has a broad pool.

Fetches daily OHLCV (lightweight, no news/sentiment/fundamentals) for all
symbols in the configured universe (nifty50 or nifty500). This gives
market-scan a real pool to rank from, instead of being limited to seed_symbols.

The constituent list is fetched live from niftyindices.com on first run
(and cached for 7 days), with the bundled subset as a safe fallback. This
gives us the actual ~500 names instead of the partial ~170 in the bundle.

The deep pass (news, sentiment, fundamentals) is handled by ingest-data,
which runs on the watchlist symbols shortlisted by market-scan.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from yolovest.data.nse_symbols import (
    fetch_live_constituent_details,
    get_universe_symbols,
)
from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)

_UNIVERSE_CACHE_TTL_DAYS = 7


class IngestUniverseSkill(SkillBase):
    name = "ingest-universe"
    description = "Fetch daily OHLCV for the full NSE scanning universe"
    trigger = SkillTrigger.CRON
    schedule = None  # set from config in __init__

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self.schedule = self.compute_schedule()

    def compute_schedule(self) -> str | None:
        return self.ctx.config.scanning.universe_cron

    def should_run(self) -> bool:
        # Don't run during market hours — avoid competing with heartbeat ingestion
        return not self.ctx.market_hours.is_market_hours()

    async def execute(self, **kwargs: Any) -> SkillResult:
        universe = kwargs.get("universe", self.ctx.config.scanning.universe)
        # Default to the configured backfill window. Daily cron re-runs are
        # mostly cache hits due to upsert idempotency, so a wider default
        # doesn't hurt and lets a manual /run ingest-universe pull the
        # full backfill window in one shot.
        default_days = self.ctx.config.market_data.backfill_days
        days = int(kwargs.get("days", default_days))

        symbols = await self._resolve_universe_symbols(universe)
        logger.info(
            "Ingesting universe '%s': %d symbols, %d days",
            universe, len(symbols), days,
        )

        results: dict[str, Any] = {
            "universe": universe,
            "total_symbols": len(symbols),
            "symbols_ingested": 0,
            "total_bars_stored": 0,
            "errors": [],
            "cache_hits": 0,
            "newly_quarantined": [],
        }

        for idx, symbol in enumerate(symbols):
            try:
                bars = await self.ctx.market_data.get_ohlcv(
                    symbol, "daily", days=days, skip_stale_check=True,
                )
                if bars:
                    count = await self.ctx.db.upsert_ohlcv(
                        symbol, "daily", bars,
                        self._ingest_source(symbol, "universe"),
                    )
                    results["total_bars_stored"] += count
                    results["symbols_ingested"] += 1
                    # Reset failure counter on a clean fetch
                    try:
                        await self.ctx.db.record_fetch_success(symbol)
                    except Exception:
                        logger.debug("record_fetch_success failed", exc_info=True)
                else:
                    # Empty result counts as a soft failure — the symbol may
                    # be delisted or the data plan doesn't cover it.
                    await self._record_failure(symbol, "no data returned", results)
                    logger.debug("No data returned for %s", symbol)

                # Broadcast progress every 10 symbols
                if (idx + 1) % 10 == 0 or idx + 1 == len(symbols):
                    await self.broadcast("ingest_progress", {
                        "skill": "ingest-universe",
                        "current": idx + 1,
                        "total": len(symbols),
                        "symbol": symbol,
                    })
            except Exception as e:
                results["errors"].append(f"{symbol}: {e}")
                logger.debug("Universe fetch failed for %s: %s", symbol, e)
                await self._record_failure(symbol, str(e), results)

        error_count = len(results["errors"])
        if error_count:
            logger.warning(
                "Universe ingestion: %d/%d symbols failed",
                error_count, len(symbols),
            )

        return SkillResult(
            success=results["symbols_ingested"] > 0,
            skill_name=self.name,
            data=results,
        )

    async def _resolve_universe_symbols(self, universe: str) -> list[str]:
        """Get constituents: cached → live fetch → bundled fallback,
        then apply user-configured quarantine replacements.

        Cached results in system_state expire after 7 days. On expiry or
        cache miss we hit niftyindices.com. If that fails (timeout, HTTP
        error, parse failure), we fall back to the bundled static list.
        """
        cache_key = f"universe_constituents:{universe}"

        raw = await self._read_universe_cache(cache_key)
        if raw:
            logger.info(
                "Using cached %s constituents (%d symbols)", universe, len(raw),
            )
        else:
            details = await fetch_live_constituent_details(universe)  # type: ignore[arg-type]
            if details:
                raw = [r["symbol"] for r in details]
                await self._write_universe_cache(cache_key, raw)
                # Persist sectors so risk-check / RiskExposureChart / sector
                # rotation analytics actually have data — the Industry column
                # in the CSV was previously discarded.
                try:
                    touched = await self.ctx.db.upsert_symbol_sectors(details)
                    if touched:
                        logger.info(
                            "Populated symbol_sectors for %s (%d rows)", universe, touched,
                        )
                except Exception as e:
                    logger.warning("Sector upsert failed for %s: %s", universe, e)
            else:
                raw = get_universe_symbols(universe)
                logger.warning(
                    "Live fetch failed for %s; using bundled list (%d symbols)",
                    universe, len(raw),
                )

        # Apply user-set quarantine replacements (e.g. ZOMATO -> ETERNAL).
        # This is the single point where replacements get applied for the
        # universe ingest path — db.set_replacement_symbol wires to here.
        resolved = await self.ctx.db.resolve_symbols_with_replacements(raw)
        if len(resolved) != len(raw):
            logger.info(
                "Applied %d quarantine replacement(s) to %s universe "
                "(%d -> %d symbols after substitution + dedup)",
                len(raw) - len(resolved), universe, len(raw), len(resolved),
            )
        return resolved

    async def _read_universe_cache(self, key: str) -> list[str] | None:
        """Return cached symbol list if fresh (≤ TTL), else None."""
        try:
            raw = await self.ctx.db.get_system_state(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            if now_ist() - fetched_at > timedelta(days=_UNIVERSE_CACHE_TTL_DAYS):
                return None
            symbols = payload.get("symbols")
            if isinstance(symbols, list) and symbols:
                return [str(s) for s in symbols]
        except Exception:
            logger.debug("Could not decode universe cache for %s", key, exc_info=True)
        return None

    async def _record_failure(
        self, symbol: str, reason: str, results: dict[str, Any],
    ) -> None:
        """Bump the per-symbol failure counter; if it hits the quarantine
        threshold, log loudly so the user knows the replacement (if any)
        needs attention.
        """
        try:
            now_quarantined = await self.ctx.db.record_fetch_failure(symbol, reason)
        except Exception:
            logger.debug("record_fetch_failure failed", exc_info=True)
            return
        if now_quarantined:
            results["newly_quarantined"].append(symbol)
            logger.warning(
                "ingest-universe: %s auto-quarantined after repeated fetch "
                "failures (last: %s). Set a replacement via the Quarantine "
                "page or it will be skipped on next run.",
                symbol, reason,
            )

    async def _write_universe_cache(self, key: str, symbols: list[str]) -> None:
        try:
            payload = json.dumps({
                "symbols": symbols,
                "fetched_at": now_ist().isoformat(),
            })
            await self.ctx.db.set_system_state(key, payload)
        except Exception:
            logger.debug("Failed to persist universe cache for %s", key, exc_info=True)
