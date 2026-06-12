"""Skill: depth-snapshot — collect order-book depth for the watchlist.

Trigger: HEARTBEAT during market hours (runs right after ingest-data;
strictly best-effort — a failure never blocks the trading pipeline).

WHY THIS EXISTS
---------------
Bar-derived features can RANK intraday outcomes (offline AUC ~0.58 on
both label formulations) but cannot PAY intraday costs at any geometry —
the experiment harness closed every bar-data avenue. The feature class
that predicts at 5-minute horizons is order flow, which Kite's quote
depth exposes live but nobody archives. This skill batches the whole
watchlist into ~one Kite quote call per heartbeat and persists the book
summary (bid/ask, full + top-5 quantities) so that, after months of
accumulation, an offline experiment can test order-flow features
honestly. Collection only: NOTHING trades on these rows.

Gates: market hours, `market_data.depth_snapshots_enabled`, and
`market_data.kite_data_enabled` (only the paid feed exposes depth).
Self-prunes to `market_data.depth_snapshot_retention_days`.
"""

import logging
from typing import Any

from yolovest.skills.base import SkillBase, SkillResult, SkillTrigger
from yolovest.timezone import now_ist

logger = logging.getLogger(__name__)


class DepthSnapshotSkill(SkillBase):
    name = "depth-snapshot"
    description = "Archive order-book depth for the watchlist (data collection)"
    trigger = SkillTrigger.HEARTBEAT
    schedule = None

    def should_run(self) -> bool:
        md = self.ctx.config.market_data
        return bool(
            self.ctx.market_hours.is_market_hours()
            and getattr(md, "depth_snapshots_enabled", False)
            and md.kite_data_enabled
        )

    async def execute(self, **kwargs: Any) -> SkillResult:
        symbols: list[str] = []
        try:
            watchlist = await self.ctx.db.get_combined_watchlist()
            symbols = [w["symbol"] for w in watchlist]
            for pos in await self.ctx.db.get_open_positions(
                mode=self.ctx.config.mode,
            ):
                sym = pos.get("symbol") if isinstance(pos, dict) else None
                if sym and sym not in symbols:
                    symbols.append(sym)
            symbols = await self.ctx.db.resolve_symbols_with_replacements(symbols)
        except Exception:
            logger.warning("depth-snapshot: symbol resolution failed", exc_info=True)
        if not symbols:
            return SkillResult(
                success=True, skill_name=self.name,
                data={"snapshots": 0, "reason": "no_symbols"},
            )

        batch_fn = getattr(self.ctx.market_data, "get_quotes_batch", None)
        if batch_fn is None:
            return SkillResult(
                success=True, skill_name=self.name,
                data={"snapshots": 0, "reason": "no_batch_quotes"},
            )
        try:
            quotes = await batch_fn(symbols)
        except Exception as e:
            # Best-effort by design — log and move on, never alert.
            logger.warning("depth-snapshot: batch quote failed: %s", e)
            return SkillResult(
                success=True, skill_name=self.name,
                data={"snapshots": 0, "reason": "quote_failed"},
            )
        if not quotes:
            return SkillResult(
                success=True, skill_name=self.name,
                data={"snapshots": 0, "reason": "empty"},
            )

        ts = now_ist().replace(second=0, microsecond=0).isoformat()
        written = await self.ctx.db.insert_depth_snapshots(ts, quotes)
        pruned = 0
        try:
            pruned = await self.ctx.db.prune_depth_snapshots(
                self.ctx.config.market_data.depth_snapshot_retention_days,
            )
        except Exception:
            logger.debug("depth-snapshot: prune failed", exc_info=True)
        logger.info(
            "depth-snapshot: archived %d/%d symbols at %s%s",
            written, len(symbols), ts,
            f" (pruned {pruned})" if pruned else "",
        )
        return SkillResult(
            success=True, skill_name=self.name,
            data={"snapshots": written, "symbols": len(symbols), "ts": ts},
        )
