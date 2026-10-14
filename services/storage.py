"""
Private reel storage.

All paths saved in the database are RELATIVE to STORAGE_ROOT and are never sent
to the browser; files are only delivered by the ownership-checked routes in
reels.py. Layout:

    users/<user_id>/reels/<reel_id>.mp4|.jpg
    guests/reels/<reel_id>.mp4|.jpg      (free-trial reels until claimed)
"""
import logging
import os
import shutil

from flask import current_app

log = logging.getLogger(__name__)


def storage_root():
    return os.path.realpath(current_app.config["STORAGE_ROOT"])


def reel_rel_dir(user_id):
    return f"users/{int(user_id)}/reels" if user_id else "guests/reels"


def abs_path(rel_path):
    """Resolve a stored relative path, refusing anything outside STORAGE_ROOT."""
    root = storage_root()
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError("Path escapes storage root")
    return full


def ensure_parent(rel_path):
    os.makedirs(os.path.dirname(abs_path(rel_path)), exist_ok=True)


def file_exists(rel_path):
    if not rel_path:
        return False
    try:
        return os.path.isfile(abs_path(rel_path))
    except ValueError:
        return False


def delete_files(*rel_paths):
    for rel in rel_paths:
        if not rel:
            continue
        try:
            path = abs_path(rel)
            if os.path.isfile(path):
                os.remove(path)
        except (OSError, ValueError):
            log.warning("Could not delete stored file %s", rel, exc_info=True)


def relocate_reel_files(reel):
    """
    Move a guest-stored reel into its owner's folder (after the guest signs in).
    Idempotent. Mutates the reel's path fields; the caller commits.
    """
    if not reel.user_id:
        return
    target_dir = reel_rel_dir(reel.user_id)
    for attr in ("video_path", "thumbnail_path"):
        rel = getattr(reel, attr)
        if not rel or not rel.startswith("guests/"):
            continue
        new_rel = f"{target_dir}/{os.path.basename(rel)}"
        try:
            src, dst = abs_path(rel), abs_path(new_rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                setattr(reel, attr, new_rel)
        except (OSError, ValueError):
            log.warning("Could not relocate %s for reel %s", rel, reel.id, exc_info=True)
