"""CSRF protection, safe-redirect checks and basic security headers."""
import hmac
import secrets
from urllib.parse import urlparse

from flask import request, session

from utils import json_error, wants_json

CSRF_KEY = "_csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_csrf_token():
    token = session.get(CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


def is_safe_next(target):
    """Only allow same-site relative paths (blocks open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def init_security(app):
    @app.before_request
    def csrf_protect():
        if request.method in SAFE_METHODS:
            return None
        sent = request.headers.get("X-CSRFToken") or request.form.get("csrf_token") or ""
        expected = session.get(CSRF_KEY, "")
        if not expected or not hmac.compare_digest(sent, expected):
            message = "Your session expired. Please refresh the page and try again."
            if wants_json():
                return json_error(400, "csrf_failed", message)
            from flask import render_template
            return render_template("error.html", code=400, title="Session expired", message=message), 400
        return None

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": get_csrf_token}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp
