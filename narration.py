"""
AI narration API: upload media -> Gemini writes a script -> the browser shows it for editing.

    POST   /api/narration/generate               upload files + settings, starts the job
    GET    /api/narration/<id>/status             poll progress / fetch the script
    POST   /api/narration/<id>/regenerate         new script from the SAME media (new settings),
                                                  or {"mode": "adjust"} to re-fit the current text
                                                  to the requested length (text only, no re-analysis)
    GET    /api/narration/<id>/media/<media_id>   private preview of an uploaded file (Range supported)
    DELETE /api/narration/<id>                    discard the draft and its files

Every route resolves the draft by (id AND owner); someone else's draft is a 404. Gemini runs in
a background thread (same pattern as reel rendering), so the browser never blocks.
"""
import json
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, jsonify, request, send_file, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

import catalog
from extensions import db
from models import MediaDraft
from services import drafts, gemini, quota
from services import media as media_svc
from utils import json_error

log = logging.getLogger(__name__)
narration_bp = Blueprint("narration", __name__)

MAX_RUNS_PER_DRAFT = 12
_UUID_RE = re.compile(r"^[0-9a-f-]{36}$", re.I)


def _owned_draft_or_404(draft_id):
    if not _UUID_RE.match(draft_id or ""):
        abort(404)
    draft = drafts.get_owned(draft_id, current_user)
    if draft is None:
        abort(404)
    return draft


def serialize_draft(draft):
    script = draft.script_text or ""
    settings = draft.settings
    words = gemini.count_words(script)
    est = None
    if settings and words:
        est = round(catalog.estimate_seconds(words, settings.get("speed", "normal"),
                                             settings.get("language", "en"), settings.get("tone")))
    return {
        "ok": True,
        "draft_id": draft.id,
        "status": draft.status,
        "stage": draft.stage,
        "error_message": draft.error_message,
        "script": script,
        "word_count": words,
        "estimated_seconds": est,
        "analysis_status": draft.analysis_status,
        "notes": draft.notes,
        "settings": settings,
        "media": drafts.public_media(draft, url_for),
    }


def _expire_if_stale(draft):
    """A server restart kills worker threads; don't leave a draft 'processing' forever."""
    if draft.status not in drafts.ACTIVE:
        return
    limit = timedelta(seconds=current_app.config["GEMINI_TIMEOUT_SECONDS"] + 180)
    last = draft.updated_at or draft.created_at
    if last and datetime.utcnow() - last > limit:
        draft.status, draft.stage = "failed", "failed"
        draft.error_message = gemini.GeminiError.MESSAGES["timeout"]
        db.session.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@narration_bp.route("/api/narration/generate", methods=["POST"])
def generate():
    cfg = current_app.config

    if not cfg["GEMINI_API_KEY"]:
        log.error("GEMINI_API_KEY is not set; AI narration is unavailable")
        return json_error(503, "not_configured", gemini.GeminiError.MESSAGES["not_configured"])

    decision = quota.evaluate_quota(current_user)
    if not decision.allowed:
        from reels import _limit_response     # same limit messages as the reel form
        return _limit_response(decision)
    if drafts.over_hourly_limit(current_user):
        return json_error(429, "too_many_requests",
                          "You've generated a lot of narrations in a short time. Please wait a little and try again.")

    settings, errors = catalog.resolve_narration_settings(request.form, cfg["MAX_NARRATION_SECONDS"])
    if errors:
        return json_error(400, "validation_error", next(iter(errors.values())), fields=errors)

    files = [f for f in request.files.getlist("files") if f and f.filename]
    is_guest = not current_user.is_authenticated
    drafts.cleanup_expired()

    draft_id = str(uuid.uuid4())
    try:
        manifest = drafts.save_uploads(files, os.path.join(cfg["STORAGE_ROOT"], "drafts", draft_id))
    except media_svc.MediaError as exc:
        return json_error(400, exc.code, exc.message, fields={"files": exc.message})
    except OSError:
        log.exception("Could not save uploaded media")
        return json_error(500, "upload_failed", "We couldn't save your files. Please try again.")

    draft = MediaDraft(
        id=draft_id,
        user_id=None if is_guest else current_user.id,          # identity from the session only
        guest_token=quota.get_guest_token(create=True) if is_guest else None,
        status="pending", stage="analyzing", media_json=json.dumps(manifest),
        settings_json=json.dumps(settings), runs=1,
    )
    db.session.add(draft)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("Could not create media draft")
        shutil.rmtree(os.path.join(cfg["STORAGE_ROOT"], "drafts", draft_id), ignore_errors=True)
        return json_error(500, "create_failed", "We couldn't start the narration right now. Please try again.")

    _start(draft_id, "generate")
    resp = jsonify(ok=True, draft_id=draft_id, status_url=url_for("narration.status", draft_id=draft_id),
                   media=drafts.public_media(draft, url_for))
    resp.status_code = 201
    return resp


@narration_bp.route("/api/narration/<draft_id>/status")
def status(draft_id):
    draft = _owned_draft_or_404(draft_id)
    _expire_if_stale(draft)
    return jsonify(serialize_draft(draft))


@narration_bp.route("/api/narration/<draft_id>/regenerate", methods=["POST"])
def regenerate(draft_id):
    cfg = current_app.config
    draft = _owned_draft_or_404(draft_id)
    _expire_if_stale(draft)
    if draft.status in drafts.ACTIVE:
        return json_error(409, "draft_busy", "Your narration is still being created.")
    if not cfg["GEMINI_API_KEY"]:
        return json_error(503, "not_configured", gemini.GeminiError.MESSAGES["not_configured"])
    if (draft.runs or 0) >= MAX_RUNS_PER_DRAFT:
        return json_error(429, "too_many_requests",
                          "That's a lot of rewrites for one set of media. Please edit the script by hand, or start a new one.")

    payload = request.get_json(silent=True) or {}
    settings, errors = catalog.resolve_narration_settings(payload, cfg["MAX_NARRATION_SECONDS"])
    if errors:
        return json_error(400, "validation_error", next(iter(errors.values())), fields=errors)

    mode = "adjust" if payload.get("mode") == "adjust" else "generate"
    script, measured = None, None
    if mode == "adjust":
        script = str(payload.get("script") or draft.script_text or "").strip()[:cfg["MAX_NARRATION_CHARS"]]
        if not script:
            return json_error(400, "validation_error", "There is no narration to adjust yet.")
        try:
            measured = float(payload.get("measured_seconds") or 0) or None
        except (TypeError, ValueError):
            measured = None
    elif not drafts.media_with_paths(draft):
        return json_error(410, "media_expired", "Your uploaded files have expired. Please add them again.")

    draft.settings_json = json.dumps(settings)
    draft.status, draft.stage, draft.error_message = "pending", "analyzing", None
    draft.runs = (draft.runs or 0) + 1
    db.session.commit()
    _start(draft.id, mode, script, measured)
    return jsonify(ok=True, draft_id=draft.id, status_url=url_for("narration.status", draft_id=draft.id)), 202


@narration_bp.route("/api/narration/<draft_id>/media/<media_id>")
def draft_media(draft_id, media_id):
    draft = _owned_draft_or_404(draft_id)
    entry = next((m for m in draft.media if m["id"] == media_id), None)
    if entry is None:
        abort(404)
    path = os.path.join(drafts.draft_dir(draft.id), os.path.basename(entry["file"]))
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype=entry["mime"], conditional=True)
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@narration_bp.route("/api/narration/<draft_id>", methods=["DELETE"])
def discard(draft_id):
    draft = _owned_draft_or_404(draft_id)
    if draft.status in drafts.ACTIVE:
        return json_error(409, "draft_busy", "Your narration is still being created.")
    drafts.remove_draft(draft)
    db.session.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------

def _start(draft_id, mode, script=None, measured=None):
    threading.Thread(target=_run_job, args=(current_app._get_current_object(), draft_id, mode, script, measured),
                     daemon=True).start()


def _set_stage(draft, stage):
    draft.stage = stage
    draft.status = "processing"
    db.session.commit()


def _run_job(app, draft_id, mode, script=None, measured=None):
    with app.app_context():
        draft = db.session.get(MediaDraft, draft_id)
        if draft is None:
            return
        cfg = app.config
        settings = draft.settings
        workdir = os.path.join(drafts.draft_dir(draft_id), "analysis")
        try:
            _set_stage(draft, "analyzing")
            if mode == "adjust":
                _set_stage(draft, "writing")
                text = gemini.adjust_narration(cfg["GEMINI_API_KEY"], cfg["GEMINI_MODEL"], script, settings,
                                               measured_seconds=measured)
                notes, analysis = draft.notes, draft.analysis_status
            else:
                result = gemini.generate_narration(
                    cfg["GEMINI_API_KEY"], cfg["GEMINI_MODEL"], drafts.media_with_paths(draft), settings, workdir,
                    on_stage=lambda s: _set_stage(draft, s), timeout=cfg["GEMINI_TIMEOUT_SECONDS"])
                text, notes, analysis = result.script, result.notes, result.analysis_status
            draft.script_text = text[:cfg["MAX_NARRATION_CHARS"]]
            draft.notes_json = json.dumps(notes)
            draft.analysis_status = analysis
            draft.status, draft.stage, draft.error_message = "ready", "ready", None
        except gemini.GeminiError as exc:
            log.error("Gemini failure for draft %s [%s]: %s", draft_id, exc.code, exc)   # technical detail: logs only
            db.session.rollback()
            draft.status, draft.stage, draft.error_message = "failed", "failed", exc.user_message
        except Exception:  # noqa: BLE001
            log.exception("Unexpected narration failure for draft %s", draft_id)
            db.session.rollback()
            draft.status, draft.stage = "failed", "failed"
            draft.error_message = gemini.GeminiError.MESSAGES["failed"]
        finally:
            try:
                db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                log.exception("Could not save narration result for draft %s", draft_id)
            shutil.rmtree(workdir, ignore_errors=True)
