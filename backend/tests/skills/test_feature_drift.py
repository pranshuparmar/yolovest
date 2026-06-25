"""Feature-distribution drift monitoring (PSI vs training stats).

Covers the three layers added for it:
  - compute_psi (pure math),
  - the feature_snapshots table roundtrip (migration 049),
  - drift-watch's end-to-end check against a stub model's training stats.
"""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from yolovest.skills.drift_watch import DriftWatchSkill, compute_psi
from yolovest.timezone import now_ist


def _edges(lo: float = 0.0, hi: float = 10.0) -> list[float]:
    """11 evenly spaced decile edges over [lo, hi]."""
    step = (hi - lo) / 10.0
    return [lo + i * step for i in range(11)]


class TestComputePsi:
    def test_in_distribution_values_score_near_zero(self):
        # Exactly 10 values per training bin → actual == expected.
        values = [0.5 + i for i in range(10) for _ in range(10)]
        psi = compute_psi(values, _edges())
        assert psi is not None and psi < 0.05

    def test_shifted_values_score_above_threshold(self):
        # Everything lands in the top bin — a maximal shift.
        psi = compute_psi([9.5] * 100, _edges())
        assert psi is not None and psi > 0.25

    def test_out_of_range_values_clip_into_end_bins(self):
        psi = compute_psi([99.0] * 100, _edges())
        assert psi is not None and psi > 0.25

    def test_constant_training_feature_returns_none(self):
        assert compute_psi([1.0] * 100, [5.0] * 11) is None

    def test_bad_edge_count_returns_none(self):
        assert compute_psi([1.0] * 100, [0.0, 1.0]) is None

    def test_empty_values_returns_none(self):
        assert compute_psi([], _edges()) is None


class TestSnapshotRoundtrip:
    @pytest.fixture
    async def db(self, tmp_path):
        from yolovest.data.db import Database

        database = Database(str(tmp_path / "test.db"))
        await database.initialize()
        yield database
        await database.close()

    async def test_upsert_get_and_prune(self, db):
        # Anchor on "now" (like get_feature_snapshots' rolling window does)
        # rather than a hard-coded date — a fixed date silently ages out of
        # the days=14 window and the test starts failing once the wall clock
        # passes it.
        recent_day = (now_ist() - timedelta(days=1)).strftime("%Y-%m-%d")
        await db.upsert_feature_snapshot(
            recent_day, "RELIANCE", "paper",
            {"rsi_14": 55.0, "atr_pct": 0.02, "non_numeric": "drop-me"},
        )
        # Same (day, symbol, mode) overwrites — one row, latest values.
        await db.upsert_feature_snapshot(
            recent_day, "RELIANCE", "paper", {"rsi_14": 60.0},
        )
        await db.upsert_feature_snapshot(
            recent_day, "TCS", "paper", {"rsi_14": 40.0},
        )
        # Different mode is scoped out.
        await db.upsert_feature_snapshot(
            recent_day, "INFY", "live", {"rsi_14": 70.0},
        )

        rows = await db.get_feature_snapshots(days=14, mode="paper")
        assert len(rows) == 2
        by_rsi = sorted(r["rsi_14"] for r in rows)
        assert by_rsi == [40.0, 60.0]
        assert all("non_numeric" not in r for r in rows)

        # Ancient rows get pruned.
        await db.upsert_feature_snapshot(
            "2020-01-01", "OLD", "paper", {"rsi_14": 1.0},
        )
        pruned = await db.prune_feature_snapshots(keep_days=30)
        assert pruned == 1


class _StubML:
    def __init__(self, stats):
        self._stats = stats

    def get_feature_stats(self, model_type):
        return self._stats if model_type == "swing" else None


@pytest.fixture
def drift_skill(app_context):
    ctx = app_context
    ctx.db.get_model_drift_stats = AsyncMock(
        return_value={"warning": None, "model_versions": []},
    )
    ctx.db.get_signal_class_counts = AsyncMock(return_value={"total": 0})
    ctx.db.prune_feature_snapshots = AsyncMock(return_value=0)
    ctx.ml = _StubML({
        "feature_names": ["rsi_14", "atr_pct"],
        "mean": [50.0, 0.02],
        "std": [10.0, 0.01],
        "deciles": [_edges(30.0, 70.0), _edges(0.005, 0.05)],
    })
    return DriftWatchSkill(ctx)


class TestDriftWatchFeatureDrift:
    async def test_shifted_live_distribution_alerts(self, drift_skill):
        # 60 snapshots with rsi_14 far above every training decile.
        drift_skill.ctx.db.get_feature_snapshots = AsyncMock(
            return_value=[{"rsi_14": 95.0, "atr_pct": 0.02}] * 60,
        )
        result = await drift_skill.execute()
        warnings = result.data["feature_drift_warnings"]
        assert any("rsi_14" in w for w in warnings)
        assert result.data["alerted"] is True
        drift_skill.ctx.notify.send.assert_awaited()
        # Observational only — must NOT auto-suspend on feature drift.
        assert result.data.get("signal_gen_suspended", False) is False

    async def test_in_distribution_live_values_stay_silent(self, drift_skill):
        snapshots = [
            {"rsi_14": 32.0 + (i % 10) * 4.0, "atr_pct": 0.006 + (i % 10) * 0.0044}
            for i in range(60)
        ]
        drift_skill.ctx.db.get_feature_snapshots = AsyncMock(
            return_value=snapshots,
        )
        result = await drift_skill.execute()
        assert result.data["feature_drift_warnings"] == []
        assert result.data["alerted"] is False

    async def test_too_few_snapshots_skips_check(self, drift_skill):
        drift_skill.ctx.db.get_feature_snapshots = AsyncMock(
            return_value=[{"rsi_14": 95.0}] * 10,
        )
        result = await drift_skill.execute()
        assert result.data["feature_drift_warnings"] == []

    async def test_pre_stats_artifact_skips_check(self, drift_skill):
        drift_skill.ctx.ml = _StubML(None)
        drift_skill.ctx.db.get_feature_snapshots = AsyncMock(
            return_value=[{"rsi_14": 95.0}] * 60,
        )
        result = await drift_skill.execute()
        assert result.data["feature_drift_warnings"] == []
