from datetime import datetime

from flask import Blueprint, render_template
from flask_login import current_user, login_required

from models import Reel
from services.quota import PLANS

account_bp = Blueprint("account", __name__)


@account_bp.route("/dashboard")
@login_required
def dashboard():
    mine = Reel.query.filter(Reel.user_id == current_user.id)  # always scoped to the signed-in user
    visible = mine.filter(Reel.status != "failed")
    month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = visible.order_by(Reel.created_at.desc()).first()
    return render_template(
        "dashboard.html",
        total=visible.count(),
        this_month=visible.filter(Reel.created_at >= month_start).count(),
        in_progress=mine.filter(Reel.status.in_(["pending", "processing"])).count(),
        last_created=last.created_at.isoformat() + "Z" if last else None,
    )


@account_bp.route("/profile")
@login_required
def profile():
    total = Reel.query.filter(Reel.user_id == current_user.id, Reel.status != "failed").count()
    plan = PLANS.get(current_user.plan, PLANS["free"])
    return render_template("profile.html", total=total, plan_label=plan["label"])
