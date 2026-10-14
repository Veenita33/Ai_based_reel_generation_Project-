"""
Idempotent schema upgrade (the project had no migration tool, only create_all).

Runs automatically on startup and can be run by hand:   python migrations.py

What it does for an EXISTING database:
  * lets reels.user_id be NULL (guest free-trial reels)  - SQLite: table rebuild,
    PostgreSQL: ALTER COLUMN
  * adds the new reels / users columns (including the AI-narration metadata) and the
    media_drafts table (created by db.create_all())
  * moves legacy reels out of the public static/reels folder into private
    storage and tags them as format "custom" 1080x1920 (what they really are)
  * marks reels that were mid-render during the upgrade as failed
Fresh databases are simply created by db.create_all().
"""
import logging
import os
import shutil

from sqlalchemy import inspect, text

from extensions import db

log = logging.getLogger(__name__)


def _col_types(dialect):
    ts = "DATETIME" if dialect in ("sqlite", "mysql") else "TIMESTAMP"
    return {
        "guest_token": "VARCHAR(64)", "reel_format": "VARCHAR(40)", "aspect_ratio": "VARCHAR(16)",
        "width": "INTEGER", "height": "INTEGER", "duration": "FLOAT",
        "voice_id": "VARCHAR(64)", "voice_name": "VARCHAR(80)",
        "video_path": "VARCHAR(400)", "thumbnail_path": "VARCHAR(400)",
        "stage": "VARCHAR(30)", "updated_at": ts,
        # AI narration metadata
        "narration_status": "VARCHAR(20)", "narration_language": "VARCHAR(16)",
        "narration_tone": "VARCHAR(40)", "narration_style": "VARCHAR(40)",
        "narration_duration_requested": "INTEGER", "narration_duration_actual": "FLOAT",
        "gemini_analysis_status": "VARCHAR(20)", "media_count": "INTEGER",
        "media_types": "VARCHAR(60)", "draft_id": "VARCHAR(36)", "generated_at": ts,
    }


def _relax_user_id(engine, dialect):
    cols = {c["name"]: c for c in inspect(engine).get_columns("reels")}
    if cols["user_id"]["nullable"]:
        return
    if dialect == "sqlite":
        log.info("Rebuilding reels table so user_id can be NULL")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reels RENAME TO reels_legacy"))
        db.create_all()
        old = {c["name"] for c in inspect(engine).get_columns("reels_legacy")}
        new = {c["name"] for c in inspect(engine).get_columns("reels")}
        shared = ", ".join(sorted(old & new))
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO reels ({shared}) SELECT {shared} FROM reels_legacy"))
            conn.execute(text("DROP TABLE reels_legacy"))
    elif dialect == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE reels ALTER COLUMN user_id DROP NOT NULL"))
    else:
        log.warning("Cannot relax reels.user_id on %s; guest reels need it nullable.", dialect)


def _add_missing_columns(engine, dialect):
    insp = inspect(engine)
    statements = []
    have = {c["name"] for c in insp.get_columns("reels")}
    for name, ctype in _col_types(dialect).items():
        if name not in have:
            statements.append(f"ALTER TABLE reels ADD COLUMN {name} {ctype}")
    if "plan" not in {c["name"] for c in insp.get_columns("users")}:
        statements.append("ALTER TABLE users ADD COLUMN plan VARCHAR(20) NOT NULL DEFAULT 'free'")
    for stmt in statements:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
            log.info("Applied: %s", stmt)
        except Exception:  # another worker may have added it first
            log.warning("Skipped (already applied?): %s", stmt)


def _backfill_legacy_reels(app):
    from models import Reel
    from services import storage

    legacy = Reel.query.filter(Reel.reel_format.is_(None)).all()
    if not legacy:
        return
    cfg = app.config
    for reel in legacy:
        reel.reel_format = "custom"
        reel.width, reel.height = cfg["REEL_WIDTH"], cfg["REEL_HEIGHT"]
        reel.aspect_ratio = "9:16"
        reel.updated_at = reel.created_at
        if reel.status in ("pending", "processing"):
            reel.status, reel.stage = "failed", "failed"
            reel.error_message = "This reel was interrupted by a system upgrade. Please create it again."
            continue
        reel.stage = reel.status
        rel_dir = storage.reel_rel_dir(reel.user_id)
        for legacy_name, attr in ((reel.filename, "video_path"), (reel.thumbnail, "thumbnail_path")):
            if not legacy_name:
                continue
            src = os.path.join(cfg["LEGACY_REELS_FOLDER"], os.path.basename(legacy_name))
            if os.path.isfile(src):
                rel = f"{rel_dir}/{os.path.basename(legacy_name)}"
                storage.ensure_parent(rel)
                shutil.move(src, storage.abs_path(rel))
                setattr(reel, attr, rel)
    db.session.commit()
    log.info("Migrated %d legacy reel(s) into private storage", len(legacy))


def upgrade_schema(app):
    engine = db.engine
    dialect = engine.dialect.name
    if "reels" in inspect(engine).get_table_names():
        _relax_user_id(engine, dialect)
    db.create_all()
    _add_missing_columns(engine, dialect)
    _backfill_legacy_reels(app)


if __name__ == "__main__":
    from app import app
    with app.app_context():
        upgrade_schema(app)
    print("Schema is up to date.")
