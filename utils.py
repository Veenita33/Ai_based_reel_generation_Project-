import re

from flask import jsonify, request


def wants_json():
    """True for fetch/XHR calls and anything under /api/."""
    return (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "XMLHttpRequest"
    )


def json_error(status, code, message, **extra):
    payload = {"ok": False, "error": code, "message": message}
    payload.update(extra)
    resp = jsonify(payload)
    resp.status_code = status
    return resp


def format_duration(seconds):
    if not seconds:
        return None
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def slugify(text, fallback="reel"):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return slug[:60] or fallback
