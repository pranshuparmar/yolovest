"""Tests for database methods (predictions, scoreboard, reports)."""

import pytest

from yolovest.data.db import Database


@pytest.fixture
async def db(tmp_path):
    """Create a fresh database with migrations applied."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


class TestInsertPrediction:
    async def test_insert_prediction(self, db):
        prediction = {
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "confidence": 0.85,
            "predicted_target": 2600.0,
            "expected_holding_period": "intraday",
            "model_version": "xgb-v1.0",
        }
        pred_id = await db.insert_prediction(prediction)

        assert pred_id.startswith("P-")
        cursor = await db.conn.execute(
            "SELECT * FROM predictions WHERE prediction_id = ?", (pred_id,)
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_insert_prediction_with_trade_id(self, db):
        # First insert a trade
        trade = {
            "trade_id": "T-PRED-001",
            "symbol": "RELIANCE",
            "signal_type": "BUY",
            "entry_price": 2500.0,
            "quantity": 10,
            "stop_loss_price": 2450.0,
            "target_price": 2600.0,
        }
        await db.insert_trade(trade)

        prediction = {
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "confidence": 0.85,
            "trade_id": "T-PRED-001",
            "expected_holding_period": "3d",
        }
        pred_id = await db.insert_prediction(prediction)

        cursor = await db.conn.execute(
            "SELECT trade_id FROM predictions WHERE prediction_id = ?", (pred_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == "T-PRED-001"


class TestScorePrediction:
    async def test_score_prediction(self, db):
        pred_id = await db.insert_prediction({
            "symbol": "RELIANCE",
            "predicted_direction": "BUY",
            "expected_holding_period": "intraday",
        })

        await db.score_prediction(
            pred_id,
            actual_price=2550.0,
            direction_correct=True,
            target_hit=False,
            actual_pnl_pct=0.02,
        )

        cursor = await db.conn.execute(
            "SELECT actual_price, direction_correct, target_hit, actual_pnl_pct "
            "FROM predictions WHERE prediction_id = ?",
            (pred_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 2550.0
        assert row[1] == 1  # True
        assert row[2] == 0  # False
        assert row[3] == pytest.approx(0.02)


class TestGetUnscoredPredictions:
    async def test_returns_elapsed_unscored(self, db):
        # Insert a prediction with past end time
        await db.conn.execute(
            "INSERT INTO predictions (prediction_id, created_at, prediction_end_time) "
            "VALUES ('P-OLD', datetime('now', '-1 day'), datetime('now', '-1 hour'))"
        )
        await db.conn.commit()

        results = await db.get_unscored_predictions()
        assert len(results) == 1
        assert results[0]["id"] == "P-OLD"

    async def test_excludes_already_scored(self, db):
        await db.conn.execute(
            "INSERT INTO predictions (prediction_id, created_at, prediction_end_time, "
            "actual_price, direction_correct) "
            "VALUES ('P-SCORED', datetime('now', '-1 day'), datetime('now', '-1 hour'), "
            "2550.0, 1)"
        )
        await db.conn.commit()

        results = await db.get_unscored_predictions()
        assert len(results) == 0

    async def test_excludes_not_elapsed(self, db):
        await db.conn.execute(
            "INSERT INTO predictions (prediction_id, created_at, prediction_end_time) "
            "VALUES ('P-FUTURE', datetime('now'), datetime('now', '+1 day'))"
        )
        await db.conn.commit()

        results = await db.get_unscored_predictions()
        assert len(results) == 0


class TestPredictionScoreboard:
    async def test_refresh_scoreboard(self, db):
        # Insert a signal + scored prediction
        await db.conn.execute(
            "INSERT INTO signals (symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version) "
            "VALUES ('RELIANCE', 'BUY', 2500, 2600, 2450, 10, 0.85, 'xgb-v1.0')"
        )
        signal_id = (await (await db.conn.execute("SELECT last_insert_rowid()")).fetchone())[0]

        await db.conn.execute(
            "INSERT INTO predictions (prediction_id, signal_id, created_at, "
            "prediction_end_time, actual_price, direction_correct, target_hit, actual_pnl_pct) "
            "VALUES ('P-SB1', ?, datetime('now', '-1 day'), datetime('now', '-1 hour'), "
            "2550.0, 1, 0, 0.02)",
            (signal_id,),
        )
        await db.conn.commit()

        await db.refresh_prediction_scoreboard()

        scoreboard = await db.get_prediction_scoreboard("overall")
        assert len(scoreboard) == 1
        assert scoreboard[0]["total_predictions"] == 1
        assert scoreboard[0]["correct_predictions"] == 1
        assert scoreboard[0]["accuracy"] == 1.0

    async def test_scoreboard_by_symbol(self, db):
        await db.conn.execute(
            "INSERT INTO signals (symbol, signal_type, entry_price, target_price, "
            "stop_loss_price, position_size, confidence_score, model_version) "
            "VALUES ('TCS', 'SELL', 3500, 3400, 3550, 5, 0.7, 'xgb-v1.0')"
        )
        signal_id = (await (await db.conn.execute("SELECT last_insert_rowid()")).fetchone())[0]

        await db.conn.execute(
            "INSERT INTO predictions (prediction_id, signal_id, created_at, "
            "prediction_end_time, actual_price, direction_correct, target_hit, actual_pnl_pct) "
            "VALUES ('P-SB2', ?, datetime('now', '-1 day'), datetime('now', '-1 hour'), "
            "3380.0, 1, 1, 0.034)",
            (signal_id,),
        )
        await db.conn.commit()

        await db.refresh_prediction_scoreboard()

        by_symbol = await db.get_prediction_scoreboard("symbol")
        assert len(by_symbol) == 1
        assert by_symbol[0]["group_key"] == "symbol:TCS"


class TestStoreReport:
    async def test_store_daily_report(self, db):
        report = {
            "type": "daily",
            "total_trades": 5,
            "total_pnl": 1500,
        }
        await db.store_report(report)

        cursor = await db.conn.execute("SELECT * FROM reports")
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["report_type"] == "daily"

    async def test_store_weekly_report(self, db):
        report = {"type": "weekly", "total_pnl": 5000}
        await db.store_report(report)

        cursor = await db.conn.execute(
            "SELECT report_type FROM reports WHERE report_type = 'weekly'"
        )
        row = await cursor.fetchone()
        assert row[0] == "weekly"


class TestGetShadowModelsReady:
    async def test_returns_ready_shadows(self, db):
        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, "
            "sharpe_ratio, status, shadow_start_date) "
            "VALUES ('intraday', 'v2.0', 'models/v2.pkl', 1.8, 'shadow', "
            "datetime('now', '-10 days'))"
        )
        await db.conn.commit()

        results = await db.get_shadow_models_ready(7)
        assert len(results) == 1
        assert results[0]["version"] == "v2.0"

    async def test_excludes_recent_shadows(self, db):
        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, "
            "sharpe_ratio, status, shadow_start_date) "
            "VALUES ('intraday', 'v3.0', 'models/v3.pkl', 2.0, 'shadow', "
            "datetime('now', '-2 days'))"
        )
        await db.conn.commit()

        results = await db.get_shadow_models_ready(7)
        assert len(results) == 0


class TestRetireModel:
    async def test_retire_model(self, db):
        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, status) "
            "VALUES ('swing', 'v1.0', 'models/v1.pkl', 'shadow')"
        )
        await db.conn.commit()

        await db.retire_model("swing", "v1.0")

        cursor = await db.conn.execute(
            "SELECT status FROM model_versions WHERE version = 'v1.0'"
        )
        row = await cursor.fetchone()
        assert row[0] == "retired"


class TestCleanupOrphanedModels:
    """Verify that orphan cleanup spares retired models so they live
    on disk until `cleanup_retired_models` runs them past the
    `retired_model_cleanup_days` grace period — and only deletes
    files that have no `model_versions` row at all.
    """

    async def test_retired_models_kept(self, db, tmp_path):
        """A model with status='retired' must keep its .pkl file."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        retired_pkl = model_dir / "swing_v20260101_120000.pkl"
        retired_pkl.write_bytes(b"fake-joblib-content")

        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, status) "
            "VALUES ('swing', 'swing_v20260101_120000', "
            "'models/swing_v20260101_120000.pkl', 'retired')"
        )
        await db.conn.commit()

        result = await db.cleanup_orphaned_models(str(model_dir))
        assert result["orphaned_files_deleted"] == 0
        assert retired_pkl.exists(), "Retired model artifact should not be deleted by orphan cleanup"

    async def test_production_and_shadow_kept(self, db, tmp_path):
        """Active production / shadow models stay on disk."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        prod_pkl = model_dir / "intraday_v20260101_120000.pkl"
        shadow_pkl = model_dir / "intraday_v20260102_120000.pkl"
        prod_pkl.write_bytes(b"prod")
        shadow_pkl.write_bytes(b"shadow")

        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, status) "
            "VALUES ('intraday', 'intraday_v20260101_120000', "
            "'models/intraday_v20260101_120000.pkl', 'production'), "
            "('intraday', 'intraday_v20260102_120000', "
            "'models/intraday_v20260102_120000.pkl', 'shadow')"
        )
        await db.conn.commit()

        result = await db.cleanup_orphaned_models(str(model_dir))
        assert result["orphaned_files_deleted"] == 0
        assert prod_pkl.exists()
        assert shadow_pkl.exists()

    async def test_truly_orphaned_file_deleted(self, db, tmp_path):
        """A .pkl with no DB row at all IS an orphan and gets deleted."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        orphan_pkl = model_dir / "intraday_v20250101_000000.pkl"
        orphan_pkl.write_bytes(b"truly-orphaned")

        # Nothing in model_versions for this filename.
        result = await db.cleanup_orphaned_models(str(model_dir))
        assert result["orphaned_files_deleted"] == 1
        assert not orphan_pkl.exists()

    async def test_mixed_states(self, db, tmp_path):
        """Orphan cleanup keeps prod / shadow / retired; deletes only no-row files."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        prod_pkl = model_dir / "intraday_v20260301_120000.pkl"
        retired_pkl = model_dir / "intraday_v20260201_120000.pkl"
        orphan_pkl = model_dir / "intraday_v20250101_120000.pkl"
        for p in (prod_pkl, retired_pkl, orphan_pkl):
            p.write_bytes(b"x")

        await db.conn.execute(
            "INSERT INTO model_versions (model_type, version, file_path, status) "
            "VALUES ('intraday', 'intraday_v20260301_120000', "
            "'models/intraday_v20260301_120000.pkl', 'production'), "
            "('intraday', 'intraday_v20260201_120000', "
            "'models/intraday_v20260201_120000.pkl', 'retired')"
        )
        await db.conn.commit()

        result = await db.cleanup_orphaned_models(str(model_dir))
        assert result["orphaned_files_deleted"] == 1
        assert prod_pkl.exists()
        assert retired_pkl.exists(), "Retired model must survive orphan sweep"
        assert not orphan_pkl.exists()
