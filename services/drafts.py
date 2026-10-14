"""
Media drafts: the uploaded videos/images/audio (and the AI narration written for them) that
live between "Generate AI Narration" and "Generate reel".

Files are stored privately at  <STORAGE_ROOT>/drafts/<draft_id>/NN_<media_id>.<ext>  - outside
static/, only reachable through the ownership-checked routes in narration.py. Stored names are
generated here (never taken from the browser), so a hostile filename cannot traverse paths.
"""
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, false
from werkzeug.utils import secure_filename

from extensions import db
from models import MediaDraft, Reel
from services import media as media_svc
from services import quota

log = logging.getLogger(__name__)

ACTIVE = ("pending", "processing")


# ---------------------------------------------------------------------------
# Ownership (same rules as reels.owner_clause)
# ---------------------------------------------------------------------------

def owner_clause(user):
    if user is not None and user.is_authenticated:
        return MediaDraft.user_id == user.id
    token = quota.get_guest_token()
    if token:
        return and_(MediaDraft.user_id.is_(None), MediaDraft.guest_token == token)
    return false()


def get_owned(draft_id, user):
    return MediaDraft.query.filter(MediaDraft.id == draft_id, owner_clause(user)).first()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def draft_dir(draft_id):
    root = os.path.realpath(os.path.join(current_app.config["STORAGE_ROOT"], "drafts"))
    full = os.path.realpath(os.path.join(root, str(uuid.UUID(str(draft_id)))))   # raises on non-UUID
    if not full.startswith(root + os.sep):
        raise ValueError("Path escapes drafts root")
    return full


def media_with_paths(draft):
    """Manifest entries plus an absolute `path`, in the order the user arranged them."""
    base = draft_dir(draft.id)
    items = []
    for m in draft.media:
        path = os.path.join(base, os.path.basename(m["file"]))
        if os.path.isfile(path):
            items.append(dict(m, path=path))
    return items


def public_media(draft, url_for):
    """What the browser may see about each file (never server paths)."""
    return [{
        "id": m["id"], "name": m["name"], "kind": m["kind"], "mime": m["mime"], "size": m["size"],
        "duration": m.get("duration"), "width": m.get("width"), "height": m.get("height"),
        "preview_url": url_for("narration.draft_media", draft_id=draft.id, media_id=m["id"]),
    } for m in draft.media]


# ---------------------------------------------------------------------------
# Saving uploads
# ---------------------------------------------------------------------------

def storage_name(original, kind, index, ext):
    """A display-safe name: ASCII only (so it is also safe to put in an AI prompt)."""
    name = secure_filename(os.path.basename(original or ""))
    if not name or "." not in name:
        name = f"{kind}-{index + 1}.{ext}"
    return name[:80]


def precheck_all(files, cfg):
    """Fast, no-disk validation of a batch. Returns [(file, kind)] or raises MediaError."""
    if not files:
        raise media_svc.MediaError("no_files", "Add at least one video, image or audio file.")
    if len(files) > cfg["MAX_MEDIA_FILES"]:
        raise media_svc.MediaError("too_many_files", f"You can add up to {cfg['MAX_MEDIA_FILES']} files per reel.")
    checked = []
    for f in files:
        kind, _ = media_svc.precheck_upload(f.stream, f.filename)
        checked.append((f, kind))
    return checked


def save_uploads(files, dest_dir):
    """
    Write the uploads to dest_dir in order and deep-validate each one (Pillow / ffmpeg).
    Returns the manifest list. On any failure the directory is removed and MediaError raised.
    """
    cfg = current_app.config
    checked = precheck_all(files, cfg)
    os.makedirs(dest_dir, exist_ok=True)
    manifest = []
    try:
        for index, (f, kind) in enumerate(checked):
            ext = media_svc.extension_of(f.filename)
            media_id = uuid.uuid4().hex[:12]
            stored = f"{index:02d}_{media_id}.{ext}"
            path = os.path.join(dest_dir, stored)
            f.save(path)
            info = media_svc.validate_file(path, f.filename)
            manifest.append({
                "id": media_id, "name": storage_name(f.filename, kind, index, ext), "kind": info.kind,
                "mime": info.mime, "size": info.size, "duration": info.duration, "width": info.width,
                "height": info.height, "has_audio": info.has_audio, "file": stored,
            })
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return manifest


# ---------------------------------------------------------------------------
# Limits and housekeeping
# ---------------------------------------------------------------------------

def over_hourly_limit(user):
    cfg = current_app.config
    is_user = user is not None and user.is_authenticated
    limit = cfg["NARRATION_DRAFTS_PER_HOUR_USER"] if is_user else cfg["NARRATION_DRAFTS_PER_HOUR_GUEST"]
    since = datetime.utcnow() - timedelta(hours=1)
    count = MediaDraft.query.filter(owner_clause(user), MediaDraft.created_at >= since).count()
    return count >= limit


def remove_draft(draft):
    try:
        shutil.rmtree(draft_dir(draft.id), ignore_errors=True)
    except ValueError:
        pass
    db.session.delete(draft)


def cleanup_expired():
    """Delete drafts (rows + files) older than DRAFT_TTL_HOURS that no running reel still needs."""
    try:
        cutoff = datetime.utcnow() - timedelta(hours=current_app.config["DRAFT_TTL_HOURS"])
        busy = db.session.query(Reel.draft_id).filter(Reel.status.in_(("pending", "processing")),
                                                       Reel.draft_id.isnot(None))
        old = MediaDraft.query.filter(MediaDraft.created_at < cutoff, MediaDraft.id.notin_(busy)).limit(50).all()
        for d in old:
            remove_draft(d)
        if old:
            db.session.commit()
    except Exception:  # noqa: BLE001 - housekeeping must never break a request
        db.session.rollback()
        log.warning("Draft cleanup failed", exc_info=True)


def claim_guest_drafts(user):
    """After a guest signs in, the media they uploaded in this browser becomes theirs."""
    token = quota.get_guest_token()
    if not token:
        return
    for d in MediaDraft.query.filter(MediaDraft.user_id.is_(None), MediaDraft.guest_token == token).all():
        d.user_id, d.guest_token = user.id, None
