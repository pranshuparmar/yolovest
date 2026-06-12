"""OpenClaw skill registry for YoloVest.

All 17 skills registered here. The agent orchestrator discovers and invokes
skills via this registry based on triggers (heartbeat, cron, event, manual).
"""

from yolovest.skills.auth_broker import AuthBrokerSkill
from yolovest.skills.auto_score import AutoScoreSkill
from yolovest.skills.backfill_data import BackfillDataSkill
from yolovest.skills.backfill_intraday import (
    BackfillIntraday1mSkill,
    BackfillIntradaySkill,
)
from yolovest.skills.base import SkillBase
from yolovest.skills.cdsl_auth_check import CdslAuthCheckSkill
from yolovest.skills.db_maintenance import DatabaseMaintenanceSkill
from yolovest.skills.depth_snapshot import DepthSnapshotSkill
from yolovest.skills.drift_watch import DriftWatchSkill
from yolovest.skills.expire_pending import ExpirePendingSkill
from yolovest.skills.funds_snapshot import FundsSnapshotSkill
from yolovest.skills.generate_signals import GenerateSignalsSkill
from yolovest.skills.health_check import HealthCheckSkill
from yolovest.skills.heartbeat_pipeline import HeartbeatPipelineSkill
from yolovest.skills.ingest_data import IngestDataSkill
from yolovest.skills.ingest_fno import IngestFnoSkill
from yolovest.skills.ingest_premarket import IngestPremarketSkill
from yolovest.skills.ingest_universe import IngestUniverseSkill
from yolovest.skills.ingest_vix import IngestVixSkill
from yolovest.skills.kill_switch import KillSwitchSkill
from yolovest.skills.llm_review import LLMReviewSkill
from yolovest.skills.market_scan import MarketScanSkill
from yolovest.skills.model_retrain import ModelRetrainSkill
from yolovest.skills.news_digest import NewsDigestSkill
from yolovest.skills.position_monitor import PositionMonitorSkill
from yolovest.skills.predict_track import PredictTrackSkill
from yolovest.skills.report_generate import ReportGenerateSkill
from yolovest.skills.reprice_pending import RepricePendingSkill
from yolovest.skills.risk_check import RiskCheckSkill
from yolovest.skills.square_off import SquareOffSkill
from yolovest.skills.trade_execute import TradeExecuteSkill

SKILL_REGISTRY: dict[str, type[SkillBase]] = {
    "auth-broker": AuthBrokerSkill,
    "backfill-data": BackfillDataSkill,
    "backfill-intraday": BackfillIntradaySkill,
    "backfill-intraday-1m": BackfillIntraday1mSkill,
    "ingest-data": IngestDataSkill,
    "ingest-fno": IngestFnoSkill,
    "ingest-premarket": IngestPremarketSkill,
    "ingest-universe": IngestUniverseSkill,
    "ingest-vix": IngestVixSkill,
    "market-scan": MarketScanSkill,
    "generate-signals": GenerateSignalsSkill,
    "risk-check": RiskCheckSkill,
    "llm-review": LLMReviewSkill,
    "trade-execute": TradeExecuteSkill,
    "position-monitor": PositionMonitorSkill,
    "square-off": SquareOffSkill,
    "predict-track": PredictTrackSkill,
    "model-retrain": ModelRetrainSkill,
    "report-generate": ReportGenerateSkill,
    "health-check": HealthCheckSkill,
    "heartbeat-pipeline": HeartbeatPipelineSkill,
    "kill-switch": KillSwitchSkill,
    "database-maintenance": DatabaseMaintenanceSkill,
    "news-digest": NewsDigestSkill,
    "drift-watch": DriftWatchSkill,
    "depth-snapshot": DepthSnapshotSkill,
    "auto-score": AutoScoreSkill,
    "expire-pending-trades": ExpirePendingSkill,
    "reprice-pending-trades": RepricePendingSkill,
    "funds-snapshot": FundsSnapshotSkill,
    "cdsl-auth-check": CdslAuthCheckSkill,
}

__all__ = ["SKILL_REGISTRY"]
