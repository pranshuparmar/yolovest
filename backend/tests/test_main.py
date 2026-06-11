"""Smoke tests for the main entry-point wiring.

These don't boot the full app; they pin the context-construction
contract: which subsystem gets a real implementation vs a stub for a
given config, that the shared Kite rate limiter is actually shared, and
that token-sync / logging setup are safe to call in any state.
"""

import logging

from pydantic import SecretStr

from yolovest.config import AppConfig
from yolovest.data.ingester import MarketDataIngester
from yolovest.main import (
    _build_broker,
    _build_llm,
    _build_market_data,
    _StubBroker,
    _StubLLM,
    _sync_kite_data_token,
    build_context,
    setup_logging,
)


def _config(**overrides) -> AppConfig:
    cfg = AppConfig()
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        if field:
            setattr(getattr(cfg, section), field, value)
        else:
            setattr(cfg, section, value)
    return cfg


class TestBuilderSelection:
    def test_no_api_key_builds_stub_broker(self):
        cfg = _config()
        cfg.broker.api_key = SecretStr("")
        assert isinstance(_build_broker(cfg), _StubBroker)

    def test_env_placeholder_builds_stub_broker(self):
        cfg = _config()
        cfg.broker.api_key = SecretStr("${KITE_API_KEY}")
        assert isinstance(_build_broker(cfg), _StubBroker)

    def test_api_key_builds_real_broker_in_config_mode(self):
        from yolovest.broker.zerodha import ZerodhaBroker

        cfg = _config(mode="paper")
        cfg.broker.api_key = SecretStr("real-key")
        cfg.broker.api_secret = SecretStr("real-secret")
        broker = _build_broker(cfg)
        assert isinstance(broker, ZerodhaBroker)
        assert broker._mode == "paper"

    def test_llm_disabled_builds_stub(self):
        cfg = _config()
        cfg.llm.enabled = False
        cfg.llm.api_key = SecretStr("some-key")
        assert isinstance(_build_llm(cfg), _StubLLM)

    def test_llm_enabled_without_key_builds_stub(self):
        cfg = _config()
        cfg.llm.enabled = True
        cfg.llm.api_key = SecretStr("")
        assert isinstance(_build_llm(cfg), _StubLLM)

    def test_market_data_default_chain(self):
        md = _build_market_data(_config())
        assert isinstance(md, MarketDataIngester)

    def test_kite_provider_heads_chain_when_enabled(self):
        from yolovest.data.kite_data import KiteDataProvider

        cfg = _config()
        cfg.market_data.kite_data_enabled = True
        cfg.broker.api_key = SecretStr("real-key")
        md = _build_market_data(cfg)
        assert isinstance(md, MarketDataIngester)
        assert isinstance(md._daily_providers[0], KiteDataProvider)


class TestBuildContext:
    def test_stub_context_without_credentials(self, tmp_path):
        cfg = _config()
        cfg.broker.api_key = SecretStr("")
        cfg.llm.api_key = SecretStr("")
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        assert isinstance(ctx.broker, _StubBroker)
        assert ctx.db is not None
        assert ctx.market_hours is not None
        assert ctx.event_bus is not None

    def test_real_broker_gets_db_and_market_data_attached(self, tmp_path):
        from yolovest.broker.zerodha import ZerodhaBroker

        cfg = _config()
        cfg.broker.api_key = SecretStr("real-key")
        cfg.broker.api_secret = SecretStr("real-secret")
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        assert isinstance(ctx.broker, ZerodhaBroker)
        assert ctx.broker._db is ctx.db
        assert ctx.broker._market_data is ctx.market_data

    def test_broker_and_kite_data_share_rate_limiter(self, tmp_path):
        from yolovest.data.kite_data import KiteDataProvider

        cfg = _config()
        cfg.broker.api_key = SecretStr("real-key")
        cfg.broker.api_secret = SecretStr("real-secret")
        cfg.market_data.kite_data_enabled = True
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        kite_provider = next(
            p for p in ctx.market_data._daily_providers
            if isinstance(p, KiteDataProvider)
        )
        assert ctx.broker._rate_limiter is kite_provider._rate_limiter


class TestTokenSync:
    def test_noop_for_stub_broker(self, tmp_path):
        cfg = _config()
        cfg.broker.api_key = SecretStr("")
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        _sync_kite_data_token(ctx)  # must not raise

    def test_noop_for_paper_token(self, tmp_path):
        cfg = _config()
        cfg.broker.api_key = SecretStr("real-key")
        cfg.broker.api_secret = SecretStr("real-secret")
        cfg.market_data.kite_data_enabled = True
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        ctx.broker._access_token = "paper_token"
        _sync_kite_data_token(ctx)
        from yolovest.data.kite_data import KiteDataProvider

        provider = next(
            p for p in ctx.market_data._daily_providers
            if isinstance(p, KiteDataProvider)
        )
        assert provider._access_token != "paper_token"

    def test_real_token_propagates_to_kite_provider(self, tmp_path):
        cfg = _config()
        cfg.broker.api_key = SecretStr("real-key")
        cfg.broker.api_secret = SecretStr("real-secret")
        cfg.market_data.kite_data_enabled = True
        cfg.database.path = str(tmp_path / "ctx.db")
        ctx = build_context(cfg)
        ctx.broker._access_token = "fresh-session-token"
        _sync_kite_data_token(ctx)
        from yolovest.data.kite_data import KiteDataProvider

        provider = next(
            p for p in ctx.market_data._daily_providers
            if isinstance(p, KiteDataProvider)
        )
        assert provider._access_token == "fresh-session-token"


class TestLoggingSetup:
    def test_reconfigure_does_not_duplicate_handlers(self, tmp_path):
        root = logging.getLogger()
        saved_handlers = root.handlers[:]
        saved_level = root.level
        try:
            root.handlers = []
            cfg = _config()
            cfg.log.log_dir = str(tmp_path / "logs")
            setup_logging(cfg)
            first_count = len(root.handlers)
            setup_logging(cfg)  # reconfigure path
            assert len(root.handlers) == first_count
        finally:
            for h in root.handlers[:]:
                if h not in saved_handlers:
                    root.removeHandler(h)
                    h.close()
            root.handlers = saved_handlers
            root.setLevel(saved_level)
