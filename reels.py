import logging
import os
import shutil
import threading
import uuid
from datetime import datetime, timedelta

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import and_, false
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

import catalog
from extensions import db
from models import GuestUsage, MediaDraft, Reel
from services import drafts, quota, storage
from services import media as media_svc
from services.tts import generate_voiceover, ElevenLabsError
from services.video import build_reel, ReelBuildError
from utils import format_duration, json_error, slugify, wants_json

log = logging.getLogger(__name__)
reels_bp = Blueprint("reels", __name__)

ACTIVE_STATUSES = ("pending", "processing")


# ----------------------------------------------------------------------------
# Ownership - the one place that decides "is this reel mine?"
# ----------------------------------------------------------------------------

def owner_clause():
    """
    SQL condition matching reels owned by the current requester, derived only from
    the server-side session (never from request parameters):
      signed in  -> reels.user_id == current_user.id
      guest      -> reels with this browser's guest token and no user yet
    """
    if current_user.is_authenticated:
        return Reel.user_id == current_user.id
    token = quota.get_guest_token()
    if token:
        return and_(Reel.user_id.is_(None), Reel.guest_token == token)
    return false()


def get_owned_reel_or_404(reel_id):
    """Someone else's reel is indistinguishable from one that doesn't exist (404)."""
    reel = Reel.query.filter(Reel.id == reel_id, owner_clause()).first()
    if reel is None:
        abort(404)
    return reel


# ----------------------------------------------------------------------------
# Serialisation
# ----------------------------------------------------------------------------

def _duration_warning(reel):
    """Set when the generated voiceover is noticeably shorter/longer than the length the user asked for."""
    requested, actual = reel.narration_duration_requested, reel.narration_duration_actual
    if not requested or not actual:
        return None
    if abs(actual - requested) <= current_app.config["NARRATION_TOLERANCE_SECONDS"]:
        return None
    draft_ok = bool(reel.draft_id) and db.session.query(MediaDraft.id).filter_by(id=reel.draft_id).first() is not None
    return {
        "requested_seconds": requested,
        "actual_seconds": round(actual),
        "message": f"The voiceover is {actual:.0f} seconds, but you asked for about {requested}.",
        "draft_id": reel.draft_id if draft_ok else None,    # lets the UI offer "adjust narration"
    }


def serialize_reel(reel):
    fmt = catalog.get_format(reel.reel_format)
    ready = reel.status == "done" and bool(reel.video_path)
    created = reel.created_at.isoformat() + "Z" if reel.created_at else None
    return {
        "id": reel.id,
        "title": reel.title,
        "status": reel.status,
        "stage": reel.stage,
        "error_message": reel.error_message,
        "platform": fmt.key,
        "platform_label": fmt.label,
        "platform_short": fmt.short_label,
        "aspect_ratio": reel.aspect_ratio or fmt.aspect_ratio,
        "width": reel.width,
        "height": reel.height,
        "resolution": reel.resolution,
        "duration": reel.duration,
        "duration_label": format_duration(reel.duration),
        "voice_name": reel.voice_name,
        "narration": {
            "status": reel.narration_status,
            "language": reel.narration_language,
            "tone": reel.narration_tone,
            "style": reel.narration_style,
            "requested_seconds": reel.narration_duration_requested,
            "actual_seconds": reel.narration_duration_actual,
            "analysis_status": reel.gemini_analysis_status,
            "media_count": reel.media_count,
            "media_types": (reel.media_types or "").split(",") if reel.media_types else [],
        },
        "duration_warning": _duration_warning(reel),
        "created_at": created,
        "video_url": url_for("reels.reel_video", reel_id=reel.id) if ready else None,
        "thumbnail_url": url_for("reels.reel_thumbnail", reel_id=reel.id) if reel.thumbnail_path else None,
        "download_url": url_for("reels.reel_download", reel_id=reel.id) if ready else None,
        "detail_url": url_for("reels.reel_detail", reel_id=reel.id),
    }


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------

def _trial_context():
    decision = quota.evaluate_quota(current_user)
    return {
        "is_guest": decision.is_guest,
        "free_used": decision.is_guest and not decision.allowed,
        "allowed": decision.allowed,
        "limit": decision.limit,
    }


@reels_bp.route("/")
def home():
    return render_template("index.html", formats=catalog.format_choices(), trial=_trial_context())


@reels_bp.route("/gallery")
@reels_bp.route("/my-reels")
@login_required
def gallery():
    return render_template("gallery.html", formats=catalog.format_choices())


@reels_bp.route("/reel/<reel_id>")
def reel_detail(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    return render_template("reel.html", reel=serialize_reel(reel), is_guest=reel.user_id is None)


@reels_bp.route("/reel/<reel_id>/processing")
def processing(reel_id):
    # Kept for old links: progress is now shown inline on the create page.
    reel = get_owned_reel_or_404(reel_id)
    return redirect(url_for("reels.create", reel=reel.id))


# ----------------------------------------------------------------------------
# Secure file delivery (ownership checked, Range requests supported)
# ----------------------------------------------------------------------------

def _send_stored(rel_path, mimetype, download_name=None, as_attachment=False):
    if not rel_path:
        abort(404)
    try:
        path = storage.abs_path(rel_path)
    except ValueError:
        abort(404)
    if not os.path.isfile(path):
        abort(404)
    resp = send_file(path, mimetype=mimetype, as_attachment=as_attachment,
                     download_name=download_name, conditional=True)
    resp.headers["Cache-Control"] = "private, no-cache"
    return resp


@reels_bp.route("/reel/<reel_id>/video")
def reel_video(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    if reel.status != "done":
        abort(404)
    return _send_stored(reel.video_path, "video/mp4")


@reels_bp.route("/reel/<reel_id>/thumbnail")
def reel_thumbnail(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    return _send_stored(reel.thumbnail_path, "image/jpeg")


@reels_bp.route("/reel/<reel_id>/download")
def reel_download(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    if reel.status != "done":
        abort(404)
    return _send_stored(reel.video_path, "video/mp4", as_attachment=True,
                        download_name=f"{slugify(reel.title)}.mp4")


# ----------------------------------------------------------------------------
# JSON API
# ----------------------------------------------------------------------------

@reels_bp.route("/api/reels")
@login_required
def api_list_reels():
    """The signed-in user's reels - filtered, sorted and paginated in SQL."""
    query = Reel.query.filter(Reel.user_id == current_user.id)

    search = request.args.get("q", "").strip()[:100]
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(Reel.title.ilike(f"%{escaped}%", escape="\\"))

    platform = request.args.get("platform", "").strip()
    if platform and platform != "all":
        if platform not in catalog.PLATFORM_FORMATS:
            return json_error(400, "invalid_platform", "Unknown platform filter.")
        query = query.filter(Reel.reel_format == platform)

    if request.args.get("sort", "newest") == "oldest":
        query = query.order_by(Reel.created_at.asc(), Reel.id.asc())
    else:
        query = query.order_by(Reel.created_at.desc(), Reel.id.desc())

    try:
        page = max(int(request.args.get("page", 1)), 1)
        per_page = min(max(int(request.args.get("per_page", 12)), 1), 48)
    except ValueError:
        return json_error(400, "invalid_paging", "Invalid page settings.")

    total = query.count()
    items = query.offset((page - 1) * per_page).limit(per_page).all()
    for reel in items:
        _expire_if_stale(reel)
    return jsonify(ok=True, items=[serialize_reel(r) for r in items], total=total,
                   page=page, has_more=page * per_page < total)


@reels_bp.route("/api/reel/<reel_id>")
def api_reel_detail(reel_id):
    return jsonify(ok=True, reel=serialize_reel(get_owned_reel_or_404(reel_id)))


@reels_bp.route("/api/reel/<reel_id>/status")
def reel_status(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    _expire_if_stale(reel)
    return jsonify(ok=True, **serialize_reel(reel))


@reels_bp.route("/api/reel/<reel_id>", methods=["PATCH"])
def api_rename_reel(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return json_error(400, "validation_error", "Give your reel a title.")
    reel.title = title[:200]
    db.session.commit()
    return jsonify(ok=True, reel=serialize_reel(reel))


@reels_bp.route("/api/reel/<reel_id>", methods=["DELETE"])
def api_delete_reel(reel_id):
    reel = get_owned_reel_or_404(reel_id)
    if reel.status in ACTIVE_STATUSES:
        return json_error(409, "reel_busy", "This reel is still being created. Try again once it finishes.")
    storage.delete_files(reel.video_path, reel.thumbnail_path)
    db.session.delete(reel)
    db.session.commit()
    return jsonify(ok=True)


# ----------------------------------------------------------------------------
# Creating a reel
# ----------------------------------------------------------------------------

def _list_songs():
    folder = current_app.config["SONGS_FOLDER"]
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith((".mp3", ".wav", ".m4a")))


@reels_bp.route("/create", methods=["GET", "POST"])
def create():
    # Open to everyone: guests get a free reel, enforced server-side in _handle_create.
    if request.method == "POST":
        return _handle_create()
    return render_template(
        "create.html",
        formats=catalog.format_choices(),
        custom_presets=catalog.CUSTOM_PRESETS,
        voices=catalog.get_voices(current_app.config),
        songs=_list_songs(),
        default_format=catalog.DEFAULT_FORMAT_KEY,
        trial=_trial_context(),
        max_clips=current_app.config["MAX_MEDIA_FILES"],
        narration=_narration_context(),
        max_chars=current_app.config["MAX_NARRATION_CHARS"],
        limits={"min": catalog.MIN_DIMENSION, "max": catalog.MAX_DIMENSION},
    )


def _narration_context():
    cfg = current_app.config
    return {
        "enabled": bool(cfg["GEMINI_API_KEY"]),
        "languages": catalog.NARRATION_LANGUAGES, "default_language": catalog.DEFAULT_NARRATION_LANGUAGE,
        "tones": catalog.NARRATION_TONES, "default_tone": catalog.DEFAULT_TONE,
        "styles": catalog.NARRATION_STYLES, "default_style": catalog.DEFAULT_STYLE,
        "audiences": catalog.NARRATION_AUDIENCES, "default_audience": catalog.DEFAULT_AUDIENCE,
        "speeds": catalog.SPEAKING_SPEEDS, "default_speed": catalog.DEFAULT_SPEED,
        "durations": catalog.NARRATION_DURATION_PRESETS, "default_seconds": catalog.DEFAULT_NARRATION_SECONDS,
        "min_seconds": catalog.MIN_NARRATION_SECONDS, "max_seconds": cfg["MAX_NARRATION_SECONDS"],
        "image_fits": catalog.IMAGE_FIT_MODES, "default_image_fit": catalog.DEFAULT_IMAGE_FIT,
        "image_seconds": cfg["DEFAULT_IMAGE_SECONDS"],
        "min_image_seconds": catalog.MIN_IMAGE_SECONDS, "max_image_seconds": catalog.MAX_IMAGE_SECONDS,
        "tolerance": cfg["NARRATION_TOLERANCE_SECONDS"],
        "client": catalog.narration_client_config(),
        "limits_mb": {"video": cfg["MAX_VIDEO_MB"], "image": cfg["MAX_IMAGE_MB"], "audio": cfg["MAX_AUDIO_MB"]},
        # the subset create.js needs (nothing secret in here)
        "js": {
            "enabled": bool(cfg["GEMINI_API_KEY"]),
            "estimate": catalog.narration_client_config(),
            "limitsMb": {"video": cfg["MAX_VIDEO_MB"], "image": cfg["MAX_IMAGE_MB"], "audio": cfg["MAX_AUDIO_MB"]},
            "extensions": {"video": sorted(cfg["ALLOWED_VIDEO_EXTENSIONS"]), "image": sorted(cfg["ALLOWED_IMAGE_EXTENSIONS"]),
                           "audio": sorted(cfg["ALLOWED_AUDIO_EXTENSIONS"])},
            "tolerance": cfg["NARRATION_TOLERANCE_SECONDS"],
            "minSeconds": catalog.MIN_NARRATION_SECONDS, "maxSeconds": cfg["MAX_NARRATION_SECONDS"],
            "defaultSeconds": catalog.DEFAULT_NARRATION_SECONDS,
        },
    }


def _limit_response(decision):
    if decision.is_guest:
        message = "Your free reel has been used. Sign in or create an account to make more."
        extra = dict(
            login_url=url_for("auth.login", next=url_for("reels.create"), **{"from": "free"}),
            register_url=url_for("auth.register", next=url_for("reels.create"), **{"from": "free"}),
        )
    else:
        message = "You've reached your reel limit for this month."
        extra = {}
    if wants_json():
        return json_error(401 if decision.is_guest else 403, decision.reason, message, **extra)
    flash(message, "warning")
    return redirect(url_for("reels.create"))


def _validate_create_form(cfg):
    """Returns (clean_values, errors{field: message})."""
    errors, clean = {}, {}

    clean["title"] = (request.form.get("title", "").strip() or "My Reel")[:200]

    text = request.form.get("text", "").strip()
    if not text:
        errors["text"] = "Write the narration you want spoken over your clips."
    elif len(text) > cfg["MAX_NARRATION_CHARS"]:
        errors["text"] = f"Narration is limited to {cfg['MAX_NARRATION_CHARS']} characters."
    clean["text"] = text

    try:
        clean["format"] = catalog.resolve_format(
            request.form.get("platform"), request.form.get("width"), request.form.get("height"))
    except ValueError as exc:
        errors["platform"] = str(exc)

    voice_id = request.form.get("voice", "").strip() or cfg.get("ELEVENLABS_VOICE_ID")
    voice = catalog.find_voice(cfg, voice_id)
    if voice is None:
        errors["voice"] = "Please choose one of the available voices."
    clean["voice"] = voice

    song = request.form.get("song", "").strip() or None
    if song and song not in _list_songs():
        errors["song"] = "That background song isn't available."
    clean["song"] = song

    # ---- render options (missing fields keep the pre-upgrade behaviour) ----
    def number(field, default, lo, hi, label):
        raw = request.form.get(field, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is None or not (lo <= value <= hi):
            errors[field] = f"{label} must be between {lo:g} and {hi:g}."
            return default
        return value

    clean["image_seconds"] = number("image_seconds", cfg["DEFAULT_IMAGE_SECONDS"],
                                    catalog.MIN_IMAGE_SECONDS, catalog.MAX_IMAGE_SECONDS, "Image duration")
    clean["music_volume"] = number("music_volume", 18, 0, 100, "Music volume") / 100
    clean["original_volume"] = number("original_volume", 0, 0, 100, "Original audio volume") / 100
    clean["image_fit"] = request.form.get("image_fit", "").strip() or catalog.DEFAULT_IMAGE_FIT
    if clean["image_fit"] not in catalog.IMAGE_FIT_MODES:
        errors["image_fit"] = "Choose how images should fit the frame."
    clean["use_audio"] = request.form.get("use_audio", "1") != "0"

    # ---- media: either a previously analysed draft, or files uploaded with this request ----
    clean["draft"], clean["files"], clean["draft_items"] = None, [], []
    draft_id = request.form.get("draft_id", "").strip()
    if draft_id:
        draft = drafts.get_owned(draft_id, current_user) if len(draft_id) == 36 else None
        if draft is None:
            errors["files"] = "Your uploaded media is no longer available. Please add it again."
        elif draft.status in drafts.ACTIVE:
            errors["files"] = "Your narration is still being created. Please wait a moment."
        elif Reel.query.filter(Reel.draft_id == draft.id, Reel.status.in_(ACTIVE_STATUSES)).first():
            errors["files"] = "A reel is already being created from these files."
        else:
            items = drafts.media_with_paths(draft)
            if len(items) != len(draft.media):
                errors["files"] = "Some of your uploaded files have expired. Please add them again."
            elif not any(i["kind"] in ("video", "image") for i in items):
                errors["files"] = "Add at least one video or image."
            clean["draft"], clean["draft_items"] = draft, items
    else:
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            for key in request.files:  # legacy field names file1, file2, ...
                if key.startswith("file") and request.files[key].filename:
                    files.append(request.files[key])
        try:
            checked = drafts.precheck_all(files, cfg)
            if not any(kind in ("video", "image") for _, kind in checked):
                errors["files"] = "Add at least one video or image."
        except media_svc.MediaError as exc:
            errors["files"] = exc.message
        clean["files"] = files
    return clean, errors


def _handle_create():
    cfg = current_app.config

    decision = quota.evaluate_quota(current_user)
    if not decision.allowed:
        return _limit_response(decision)

    clean, errors = _validate_create_form(cfg)
    if errors:
        message = next(iter(errors.values()))
        if wants_json():
            return json_error(400, "validation_error", message, fields=errors)
        flash(message, "danger")
        return redirect(url_for("reels.create"))

    fmt, voice = clean["format"], clean["voice"]
    draft = clean["draft"]
    is_guest = not current_user.is_authenticated
    guest_token = quota.get_guest_token(create=True) if is_guest else None
    reel_id = str(uuid.uuid4())

    # Where did the narration come from? "ai" = Gemini's text untouched, "ai_edited" = the user
    # changed it, "manual" = they wrote it themselves.
    narration_status, settings = "manual", {}
    if draft is not None and (draft.script_text or "").strip():
        same = " ".join(clean["text"].split()) == " ".join(draft.script_text.split())
        narration_status = "ai" if same else "ai_edited"
        settings = draft.settings
    kinds = [i["kind"] for i in clean["draft_items"]] if draft is not None else None

    reel = Reel(
        id=reel_id,
        user_id=None if is_guest else current_user.id,   # identity comes from the session only
        guest_token=guest_token,
        title=clean["title"], script_text=clean["text"], song=clean["song"],
        voice_id=voice["id"], voice_name=voice["name"],
        reel_format=fmt.key, aspect_ratio=fmt.aspect_ratio, width=fmt.width, height=fmt.height,
        status="pending", stage="queued",
        narration_status=narration_status,
        narration_language=settings.get("language"), narration_tone=settings.get("tone"),
        narration_style=settings.get("style"),
        narration_duration_requested=settings.get("duration_seconds"),
        gemini_analysis_status=draft.analysis_status if draft is not None else None,
        draft_id=draft.id if draft is not None else None,
    )
    if kinds is not None:
        reel.media_count, reel.media_types = len(kinds), ",".join(sorted(set(kinds)))
    db.session.add(reel)
    if is_guest:
        quota.reserve_guest_generation(reel_id, guest_token)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        log.exception("Could not create reel record")
        return _create_failed()

    job_dir = os.path.join(cfg["UPLOAD_FOLDER"], reel_id)
    try:
        os.makedirs(job_dir, exist_ok=True)
        if draft is not None:
            items = clean["draft_items"]                        # already on disk, in the user's order
        else:
            manifest = drafts.save_uploads(clean["files"], os.path.join(job_dir, "media"))
            base = os.path.join(job_dir, "media")
            items = [dict(m, path=os.path.join(base, m["file"])) for m in manifest]
            reel.media_count = len(items)
            reel.media_types = ",".join(sorted({m["kind"] for m in items}))
            db.session.commit()
    except media_svc.MediaError as exc:
        _discard_reel(reel_id, job_dir)
        if wants_json():
            return json_error(400, exc.code, exc.message, fields={"files": exc.message})
        flash(exc.message, "danger")
        return redirect(url_for("reels.create"))
    except Exception:  # noqa: BLE001
        log.exception("Could not save uploaded media")
        _discard_reel(reel_id, job_dir)
        return _create_failed()

    visuals = [{"path": i["path"], "kind": i["kind"]} for i in items if i["kind"] in ("video", "image")]
    audio_paths = [i["path"] for i in items if i["kind"] == "audio"]
    opts = {k: clean[k] for k in ("image_seconds", "image_fit", "music_volume", "original_volume", "use_audio")}

    threading.Thread(
        target=_process_reel_job,
        args=(current_app._get_current_object(), reel_id, visuals, audio_paths, clean["text"],
              voice["id"], clean["song"], fmt.width, fmt.height, opts),
        daemon=True,
    ).start()

    if wants_json():
        resp = jsonify(ok=True, reel_id=reel_id,
                       status_url=url_for("reels.reel_status", reel_id=reel_id),
                       is_guest=is_guest)
        resp.status_code = 201
        return resp
    return redirect(url_for("reels.create", reel=reel_id))


def _create_failed():
    message = "We couldn't start your reel right now. Please try again."
    if wants_json():
        return json_error(500, "create_failed", message)
    flash(message, "danger")
    return redirect(url_for("reels.create"))


def _discard_reel(reel_id, job_dir):
    try:
        quota.release_guest_generation(reel_id)
        Reel.query.filter_by(id=reel_id).delete()
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
    shutil.rmtree(job_dir, ignore_errors=True)


# ----------------------------------------------------------------------------
# Background job
# ----------------------------------------------------------------------------

def _set_stage(reel, stage, status=None):
    reel.stage = stage
    if status:
        reel.status = status
    db.session.commit()


def _fail(reel, user_message):
    reel.status, reel.stage, reel.error_message = "failed", "failed", user_message
    # a failed reel shouldn't cost the visitor their free one
    quota.release_guest_generation(reel.id)


def _process_reel_job(app, reel_id, visuals, audio_paths, text, voice_id, song, width, height, opts):
    """TTS -> render -> store privately -> update DB. Runs in a background thread.

    visuals: ordered [{"path", "kind": "video"|"image"}]; audio_paths: uploaded audio used as background music."""
    with app.app_context():
        reel = db.session.get(Reel, reel_id)
        job_dir = os.path.join(app.config["UPLOAD_FOLDER"], reel_id)
        if not reel:
            shutil.rmtree(job_dir, ignore_errors=True)
            return

        out_rel = thumb_rel = None
        try:
            _set_stage(reel, "voiceover", status="processing")
            voiceover_path = os.path.join(job_dir, "voiceover.mp3")
            generate_voiceover(
                text=text, output_path=voiceover_path,
                api_key=app.config["ELEVENLABS_API_KEY"], voice_id=voice_id,
                model_id=app.config["ELEVENLABS_MODEL_ID"],
            )
            # How long did the voice really come out? Recorded so the UI can flag a big miss vs. the
            # length the user asked for (see _duration_warning).
            actual = media_svc.probe_duration(voiceover_path)
            if actual:
                reel.narration_duration_actual = round(actual, 1)
                db.session.commit()
                requested = reel.narration_duration_requested
                if requested and abs(actual - requested) > app.config["NARRATION_TOLERANCE_SECONDS"]:
                    log.info("Reel %s: voiceover is %.1fs but %ss was requested", reel_id, actual, requested)

            song_path = None
            if song:
                candidate = os.path.join(app.config["SONGS_FOLDER"], secure_filename(song))
                if os.path.exists(candidate):
                    song_path = candidate

            rel_dir = storage.reel_rel_dir(reel.user_id)
            out_rel, thumb_rel = f"{rel_dir}/{reel_id}.mp4", f"{rel_dir}/{reel_id}.jpg"
            storage.ensure_parent(out_rel)

            result = build_reel(
                video_paths=[v["path"] for v in visuals if v["kind"] == "video"],
                voiceover_path=voiceover_path,
                output_path=storage.abs_path(out_rel), width=width, height=height,
                song_path=song_path, thumbnail_path=storage.abs_path(thumb_rel),
                on_stage=lambda name: _set_stage(reel, name),
                media_items=visuals,
                music_paths=audio_paths if opts["use_audio"] else [],
                image_seconds=opts["image_seconds"], image_fit=opts["image_fit"],
                song_volume=opts["music_volume"], original_volume=opts["original_volume"],
            )

            _set_stage(reel, "finalizing")
            reel.video_path = out_rel
            reel.filename = os.path.basename(out_rel)
            reel.thumbnail_path = thumb_rel if storage.file_exists(thumb_rel) else None
            reel.duration = result["duration"]
            reel.generated_at = datetime.utcnow()
            reel.status, reel.stage, reel.error_message = "done", "done", None

        except ElevenLabsError as exc:
            log.error("ElevenLabs failure for reel %s: %s", reel_id, exc)
            _fail(reel, exc.user_message)
        except ReelBuildError as exc:
            log.error("Render failure for reel %s: %s", reel_id, exc)
            detail = str(exc)
            if "Could not read video file" in detail:
                _fail(reel, "One of your clips couldn't be read. Try re-exporting it as an MP4 (H.264) and upload it again.")
            elif "Could not read image file" in detail:
                _fail(reel, "One of your images couldn't be read. Try saving it again as a JPG or PNG and upload it again.")
            else:
                _fail(reel, "We couldn't render your reel. Please try again.")
        except Exception:  # noqa: BLE001
            log.exception("Unexpected failure for reel %s", reel_id)
            db.session.rollback()
            _fail(reel, "We couldn't generate your reel right now. Please try again.")
        finally:
            try:
                db.session.commit()
                if reel.status == "failed":
                    storage.delete_files(out_rel, thumb_rel)
                elif reel.status == "done":
                    # the guest may have signed in while we were rendering
                    db.session.refresh(reel)
                    storage.relocate_reel_files(reel)
                    db.session.commit()
            except SQLAlchemyError:
                db.session.rollback()
                log.exception("Could not finalise reel %s", reel_id)
            shutil.rmtree(job_dir, ignore_errors=True)


def _expire_if_stale(reel):
    """A server restart kills render threads; don't leave such reels 'processing' forever."""
    if reel.status not in ACTIVE_STATUSES:
        return
    last = reel.updated_at or reel.created_at
    if last and datetime.utcnow() - last > timedelta(minutes=current_app.config["REEL_STALE_MINUTES"]):
        _fail(reel, "This reel took too long and was cancelled. Please try again.")
        db.session.commit()
        shutil.rmtree(os.path.join(current_app.config["UPLOAD_FOLDER"], reel.id), ignore_errors=True)
