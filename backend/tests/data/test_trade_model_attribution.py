"""Every system-origin trade must resolve to the model version that
produced it (migration 050): stamped on the trade row at execution, with
a fallback join to the producing signal for legacy rows."""

import pytest

from yolovest.data.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def _signal(symbol: str = "RELIANCE") -> dict:
    return {
        "symbol": symbol,
        "signal_type": "BUY",
        "entry_price": 100.0,
        "target_price": 105.0,
        "stop_loss_price": 98.0,
        "position_size": 10,
        "expected_holding_period": "intraday",
        "confidence_score": 0.7,
        "model_version": "swing_v20260601_060000",
        "features_snapshot": {},
        "mode": "paper",
    }


def _trade(symbol: str = "RELIANCE", **overrides) -> dict:
    base = {
        "symbol": symbol,
        "signal_type": "BUY",
        "entry_price": 100.0,
        "fill_price": 100.1,
        "quantity": 10,
        "stop_loss_price": 98.0,
        "target_price": 105.0,
        "order_id": "ORD-1",
        "sl_order_id": None,
        "product": "MIS",
        "mode": "paper",
        "status": "open",
        "slippage": 0.1,
    }
    base.update(overrides)
    return base


class TestTradeModelAttribution:
    async def test_stamped_model_version_round_trips(self, db):
        await db.insert_trade(
            _trade(model_version="swing_v20260601_060000"),
        )
        rows = await db.get_trades_history(mode="paper")
        assert rows[0]["model_version"] == "swing_v20260601_060000"
        assert "signal_model_version" not in rows[0]

        detail = await db.get_trade_detail(rows[0]["trade_id"])
        assert detail["model_version"] == "swing_v20260601_060000"
        assert "signal_model_version" not in detail

    async def test_legacy_row_resolves_via_signal_join(self, db):
        # Pre-050 behaviour: trade row has no model_version of its own
        # but links to its producing signal.
        signal_id = await db.insert_signal(_signal())
        await db.insert_trade(_trade(signal_id=signal_id))
        rows = await db.get_trades_history(mode="paper")
        assert rows[0]["model_version"] == "swing_v20260601_060000"

    async def test_adopted_trade_has_no_model(self, db):
        # Adopted/manual trades aren't model-produced — stays NULL.
        await db.insert_trade(_trade(symbol="TCS"))
        rows = await db.get_trades_history(mode="paper", symbol="TCS")
        assert rows[0]["model_version"] is None
