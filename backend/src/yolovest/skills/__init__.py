"""OpenClaw skill registry for YoloVest.

All 17 skills registered here. The agent orchestrator discovers and invokes
skills via this registry based on triggers (heartbeat, cron, event, manual).
"""

from yolovest.skills.auth_broker import AuthBrokerSkill
from yolovest.skills.backfill_data import BackfillDataSkill
from yolovest.skills.backfill_intraday import BackfillIntradaySkill
from yolovest.skills.db_maintenance import DatabaseMaintenanceSkill
from yolovest.skills.drift_watch import DriftWatchSkill
from yolovest.skills.generate_signals import GenerateSignalsSkill
from yolovest.skills.health_check import HealthCheckSkill
from yolovest.skills.ingest_data import IngestDataSkill
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
from yolovest.skills.risk_check import RiskCheckSkill
from yolovest.skills.square_off import SquareOffSkill
from yolovest.skills.trade_execute import TradeExecuteSkill

SKILL_REGISTRY: dict[str, type] = {
    "auth-broker": AuthBrokerSkill,
    "backfill-data": BackfillDataSkill,
    "backfill-intraday": BackfillIntradaySkill,
    "ingest-data": IngestDataSkill,
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
    "kill-switch": KillSwitchSkill,
    "database-maintenance": DatabaseMaintenanceSkill,
    "news-digest": NewsDigestSkill,
    "drift-watch": DriftWatchSkill,
}

__all__ = ["SKILL_REGISTRY"]
