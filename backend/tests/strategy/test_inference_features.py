"""Tests for inference-time feature enrichment — the fix for the
train/inference mismatch where ~19 model features were fed as 0.0 live
(notably universe/sector breadth, whose training neutral is 0.5)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from yolovest.strategy.inference_features import enrich_features

# The 19 features that were previously zeroed at inference.
_ENRICHED_KEYS = {
    "universe_breadth", "universe_avg_return",
    "sector_breadth", "sector_avg_return", "relative_momentum",
    "bulk_deal_buy_5d", "bulk_deal_sell_5d", "bulk_deal_net_5d",
    "delivery_pct_avg_5d",
    "fb_pred_accuracy", "fb_pred_target_hit", "fb_pred_avg_pnl",
    "fb_dry_run_accuracy", "fb_dry_run_avg_move", "fb_trade_win_rate",
    "fb_trade_avg_pnl", "fb_trade_avg_slippage", "fb_recent_loss_count",
    "fb_has_data",
}


def _ctx(buy=0, sell=0, delivery=55.0):
    db = SimpleNamespace(
        count_recent_bulk_deals=AsyncMock(
            return_value={"buy_count": buy, "sell_count": sell}),
        get_recent_delivery_pct=AsyncMock(return_value=delivery),
    )
    return SimpleNamespace(db=db)


class TestEnrichFeatures:
    async def test_all_19_features_populated(self):
        feats: dict = {}
        fctx = {
            "regime": {"breadth": 0.58, "avg_return": 0.004, "sample_size": 120},
            "sector_stats": {"BANK": {"breadth": 0.7, "avg_return": 0.01, "n": 5}},
            "symbol_returns": {"HDFCBANK": 0.025},
            "sector_map": {"HDFCBANK": "BANK"},
            "feedback_data": {},
        }
        await enrich_features(_ctx(buy=3, sell=1), "HDFCBANK", feats, fctx)
        assert _ENRICHED_KEYS.issubset(feats.keys())
        assert feats["universe_breadth"] == 0.58
        assert feats["sector_breadth"] == 0.7
        assert abs(feats["relative_momentum"] - (0.025 - 0.01)) < 1e-9
        assert feats["bulk_deal_net_5d"] == 2.0
        assert feats["delivery_pct_avg_5d"] == 55.0

    async def test_neutral_defaults_are_half_not_zero(self):
        # The critical fix: when regime/sector data is absent the defaults
        # must be 0.5 (training's neutral), NOT 0.0 (the old off-distribution
        # value that compressed the model).
        feats: dict = {}
        fctx = {
            "regime": {"breadth": 0.5, "avg_return": 0.0, "sample_size": 0},
            "sector_stats": {}, "symbol_returns": {}, "sector_map": {},
            "feedback_data": {},
        }
        await enrich_features(_ctx(), "UNKNOWN", feats, fctx)
        assert feats["universe_breadth"] == 0.5
        assert feats["sector_breadth"] == 0.5
        assert feats["fb_pred_accuracy"] == 0.5  # feedback neutral, not 0.0

    async def test_bulk_deal_query_failure_is_neutral(self):
        feats: dict = {}
        ctx = _ctx()
        ctx.db.count_recent_bulk_deals = AsyncMock(side_effect=RuntimeError("db down"))
        fctx = {"regime": {"sample_size": 0}, "sector_stats": {},
                "symbol_returns": {}, "sector_map": {}, "feedback_data": {}}
        await enrich_features(ctx, "X", feats, fctx)
        assert feats["bulk_deal_buy_5d"] == 0.0  # fail-open, doesn't crash
