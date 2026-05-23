"""Offline model trainer — run model-retrain standalone on a beefier box.

Workflow for training on a higher-memory machine without running the
full app (dashboard / broker / heartbeat):

1. On the production server, create a backup (VACUUM INTO produces a
   portable, self-contained .db):  the nightly database-maintenance
   already does this, or trigger it via the dashboard. Copy the
   backup .db + the models/ dir to the training box.

2. On the training box, point this trainer at the copied DB:

       python -m yolovest.train_offline --config config.yaml \
           --db /path/to/backup.db --models-out ./models_out \
           --max-training-days 1825

   It runs the exact same ModelRetrainSkill the live system uses (so
   the leak-free walk-forward CV, threshold tuning, and calibration
   are identical), writes the .pkl artifacts to --models-out, and
   emits a manifest.json describing each trained model.

3. Copy --models-out (the .pkl files + manifest.json) back to the
   production server's models/ dir, then register + promote them via
   POST /api/models/import (see dashboard.app) — that hot-reloads the
   running server in-process.

This trainer does NOT touch the production registry: it runs against
the copied DB, so the model_versions rows it writes live in that
copy. The import endpoint is what registers the artifact on prod.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from yolovest.config import apply_db_config, load_config
from yolovest.main import _build_db, build_context, setup_logging

logger = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except Exception:
        logger.exception("Failed to load config from %s", args.config)
        return 1

    # Overrides that matter for an offline run.
    if args.db:
        config.database.path = args.db
    if args.models_out:
        # Train artifacts land here; copy them to prod afterwards.
        Path(args.models_out).mkdir(parents=True, exist_ok=True)
    if args.max_training_days:
        config.retraining.max_training_days = args.max_training_days

    setup_logging(config)

    db = _build_db(config)
    await db.initialize()
    try:
        # Apply DB-stored config so the offline run mirrors prod's
        # effective settings (thresholds, holding periods, etc.).
        try:
            db_values = await db.get_all_config()
            config = apply_db_config(config, db_values)
        except Exception:
            logger.warning("Could not apply DB config; using file config", exc_info=True)

        ctx = build_context(config, db=db)
        # Point the ML provider at the output dir so artifacts land
        # where the user expects to collect them.
        if args.models_out and hasattr(ctx.ml, "model_dir"):
            ctx.ml.model_dir = Path(args.models_out)
            ctx.ml.model_dir.mkdir(parents=True, exist_ok=True)

        from yolovest.skills.model_retrain import ModelRetrainSkill

        skill = ModelRetrainSkill(ctx)
        logger.info(
            "Offline training start: db=%s, max_training_days=%d, models_out=%s",
            config.database.path, config.retraining.max_training_days,
            getattr(ctx.ml, "model_dir", "?"),
        )
        result = await skill.execute()

        if not result.success:
            logger.error("Offline training failed: %s", result.error)
            return 1

        # Build a manifest of the trained artifacts so the import step
        # on prod knows what to register.
        results = result.data.get("results", {}) if result.data else {}
        manifest: dict[str, object] = {"models": []}
        for model_type, info in results.items():
            if not isinstance(info, dict) or "version" not in info:
                continue
            version = info["version"]
            manifest["models"].append({
                "model_type": model_type,
                "version": version,
                "file": f"{version}.pkl",
                "metrics": info.get("metrics", {}),
            })

        out_dir = Path(args.models_out) if args.models_out else Path(ctx.ml.model_dir)
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str))
        logger.info(
            "Offline training done. %d model(s) written; manifest at %s",
            len(manifest["models"]), manifest_path,
        )
        for m in manifest["models"]:
            logger.info("  %s -> %s", m["model_type"], m["file"])
        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train YoloVest ML models offline against a copied DB.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--db", default=None, help="Override database.path (e.g. a copied backup .db)")
    parser.add_argument("--models-out", default="./models_out", help="Directory to write trained .pkl artifacts + manifest")
    parser.add_argument("--max-training-days", type=int, default=None, help="Override retraining.max_training_days")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
