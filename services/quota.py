"""
Who may generate a reel right now?

This is the single place that answers that question, so moving from
"1 free reel for guests" to free/premium plans, monthly limits or credits means
editing PLANS and `evaluate_quota` - not the routes.
"""
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime

from flask import current_app, request, session
from sqlalchemy import or_

from extensions import db
from models import GuestUsage, Reel

log = logging.getLogger(__name__)

GUEST_SESSION_KEY = "guest_token"

# monthly_reels=None means unlimited. Add "premium", credits etc. here later and
# set User.plan accordingly.
PLANS = {
    "free": {"label": "Free", "monthly_reels": None},
}


@dataclass
class QuotaDecision:
    allowed: bool
    is_guest: bool
    reason: str | None = None       # e.g. "free_limit_reached", "monthly_limit_reached"
    limit: int | None = None
    remaining: int | None = None    # None = unlimited


# ---- guest identity -----------------------------------------------------------

def get_guest_token(create=False):
    token = session.get(GUEST_SESSION_KEY)
    if not token and create:
        token = secrets.token_hex(16)
        session[GUEST_SESSION_KEY] = token
        session.permanent = True
    return token


def client_ip_hash():
    ip = request.remote_addr or ""
    salt = current_app.config["SECRET_KEY"]
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def _guest_used(token):
    conds = []
    if token:
        conds.append(GuestUsage.token == token)
    if current_app.config["GUEST_TRACK_BY_IP"]:
        conds.append(GuestUsage.ip_hash == client_ip_hash())
    if not conds:
        return 0
    return GuestUsage.query.filter(or_(*conds)).count()


# ---- decision ---------------------------------------------------------------------

def evaluate_quota(user=None):
    if user is not None and user.is_authenticated:
        plan = PLANS.get(user.plan, PLANS["free"])
        monthly = plan["monthly_reels"]
        if monthly is None:
            return QuotaDecision(True, False)
        month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        used = Reel.query.filter(
            Reel.user_id == user.id, Reel.status != "failed", Reel.created_at >= month_start
        ).count()
        remaining = max(monthly - used, 0)
        return QuotaDecision(remaining > 0, False,
                             None if remaining else "monthly_limit_reached", monthly, remaining)

    limit = current_app.config["GUEST_FREE_REELS"]
    used = _guest_used(get_guest_token())
    remaining = max(limit - used, 0)
    return QuotaDecision(remaining > 0, True,
                         None if remaining else "free_limit_reached", limit, remaining)


def reserve_guest_generation(reel_id, token):
    """Record the free-trial use. Added to the session; the caller commits."""
    db.session.add(GuestUsage(token=token, ip_hash=client_ip_hash(), reel_id=reel_id))


def release_guest_generation(reel_id):
    """Refund the free reel when generation failed (the visitor got nothing)."""
    GuestUsage.query.filter_by(reel_id=reel_id).delete()


# ---- guest -> account ---------------------------------------------------------------

def claim_guest_reels(user):
    """Attach reels made as a guest in this browser to the account that just signed in."""
    from services import storage

    token = session.get(GUEST_SESSION_KEY)
    if not token:
        return 0
    reels = Reel.query.filter(Reel.user_id.is_(None), Reel.guest_token == token).all()
    for reel in reels:
        reel.user_id = user.id
        reel.guest_token = None
        if reel.status == "done":
            storage.relocate_reel_files(reel)
    from services import drafts   # local import: drafts imports this module
    drafts.claim_guest_drafts(user)
    db.session.commit()
    return len(reels)
