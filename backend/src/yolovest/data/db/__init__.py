"""SQLite database layer with WAL mode and migration support.

The public surface is unchanged from the original monolithic db.py:
`Database`, `DuplicateSignalError`, `DASHBOARD_READ_CONN` and the
module-level helpers all import from `yolovest.data.db` exactly as
before. Internally the class is composed from per-domain mixins so each
table family lives in its own reviewable module.
"""

from yolovest.data.db.audit import AuditMixin
from yolovest.data.db.core import (
    DASHBOARD_READ_CONN,
    DatabaseCore,
    DuplicateSignalError,
    _canonical_ohlcv_ts,
    _normalize_iso_date,
    _source_priority_sql,
)
from yolovest.data.db.dryrun import DryRunMixin
from yolovest.data.db.feature_queries import FeatureQueriesMixin
from yolovest.data.db.maintenance import MaintenanceMixin
from yolovest.data.db.market_meta import MarketMetaMixin
from yolovest.data.db.models_training import ModelsTrainingMixin
from yolovest.data.db.news_market import NewsMarketMixin
from yolovest.data.db.ohlcv import OhlcvMixin
from yolovest.data.db.pending import PendingTradesMixin
from yolovest.data.db.portfolio_analytics import PortfolioAnalyticsMixin
from yolovest.data.db.predictions import PredictionsMixin
from yolovest.data.db.quarantine import QuarantineMixin
from yolovest.data.db.reports_dashboard import ReportsDashboardMixin
from yolovest.data.db.signals import SignalsMixin
from yolovest.data.db.state_config import StateConfigMixin
from yolovest.data.db.trades import TradesMixin
from yolovest.data.db.watchlist import WatchlistMixin


class Database(
    DatabaseCore,
    StateConfigMixin,
    OhlcvMixin,
    MarketMetaMixin,
    WatchlistMixin,
    AuditMixin,
    NewsMarketMixin,
    SignalsMixin,
    ModelsTrainingMixin,
    PortfolioAnalyticsMixin,
    TradesMixin,
    PredictionsMixin,
    ReportsDashboardMixin,
    MaintenanceMixin,
    PendingTradesMixin,
    QuarantineMixin,
    DryRunMixin,
    FeatureQueriesMixin,
):
    """Async SQLite database with WAL mode, read/write separation, and
    migration support. See DatabaseCore for the connection model; each
    mixin documents its table family."""


__all__ = [
    "DASHBOARD_READ_CONN",
    "Database",
    "DuplicateSignalError",
    "_canonical_ohlcv_ts",
    "_normalize_iso_date",
    "_source_priority_sql",
]
