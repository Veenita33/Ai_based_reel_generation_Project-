import re

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User
from security import is_safe_next
from services.quota import claim_guest_reels

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _next_target():
    target = request.values.get("next", "")
    return target if is_safe_next(target) else ""


def _after_auth(user, message):
    """Attach any free-trial reel made in this browser, then continue the flow."""
    claimed = claim_guest_reels(user)
    if claimed:
        message += " Your free reel is now in your gallery."
    flash(message, "success")
    return redirect(_next_target() or url_for("account.dashboard"))


def _ctx():
    return {"next": _next_target(), "from_free": request.values.get("from") == "free"}


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(_next_target() or url_for("account.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        error = None
        if not username or len(username) < 3:
            error = "Username must be at least 3 characters."
        elif not EMAIL_RE.match(email):
            error = "Please enter a valid email address."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.query.filter_by(username=username).first():
            error = "That username is already taken."
        elif User.query.filter_by(email=email).first():
            error = "An account with that email already exists."

        if error:
            flash(error, "danger")
            return render_template("register.html", username=username, email=email, **_ctx())

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return _after_auth(user, "Welcome! Your account has been created.")

    return render_template("register.html", **_ctx())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_next_target() or url_for("account.dashboard"))

    if request.method == "POST":
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())
        ).first()

        if user and user.check_password(password):
            login_user(user, remember=bool(request.form.get("remember")))
            return _after_auth(user, f"Welcome back, {user.username}!")

        flash("Invalid username/email or password.", "danger")
        return render_template("login.html", username=identifier, **_ctx())

    return render_template("login.html", **_ctx())


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("reels.home"))
