import logging
import os

from flask import Flask, flash, redirect, render_template, request, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from config import Config
from extensions import db, login_manager
from models import User
from security import init_security
from utils import json_error, wants_json

ERROR_COPY = {
    400: ("Something went wrong", "That request couldn't be processed. Please check it and try again."),
    401: ("Sign in required", "Please sign in to continue."),
    403: ("Not allowed", "You don't have permission to do that."),
    404: ("Page not found", "We couldn't find what you were looking for."),
    405: ("Not allowed", "That action isn't available here."),
    413: ("Upload too large", "Those files are too big. Try fewer or smaller files."),
    500: ("We hit a snag", "Something went wrong on our side. Please try again."),
}


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    if app.config["SECRET_KEY"] == "dev-secret-key-change-this":
        app.logger.warning("SECRET_KEY is the insecure default - set a real one in production.")
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    for folder in (app.config["STORAGE_ROOT"], app.config["UPLOAD_FOLDER"], app.config["SONGS_FOLDER"]):
        os.makedirs(folder, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    init_security(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @login_manager.unauthorized_handler
    def unauthorized():
        if wants_json():
            return json_error(401, "auth_required", "Please sign in to continue.",
                              login_url=url_for("auth.login"))
        flash("Please sign in to access that page.", "warning")
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    from auth import auth_bp
    from reels import reels_bp
    from narration import narration_bp
    from account import account_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(reels_bp)
    app.register_blueprint(narration_bp)
    app.register_blueprint(account_bp)

    register_error_handlers(app)

    with app.app_context():
        from migrations import upgrade_schema
        upgrade_schema(app)

    return app


def register_error_handlers(app):
    def respond(status, code=None, message=None):
        title, default_msg = ERROR_COPY.get(status, ERROR_COPY[500])
        message = message or default_msg
        if wants_json():
            return json_error(status, code or title.lower().replace(" ", "_"), message)
        return render_template("error.html", code=status, title=title, message=message), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return respond(exc.code or 500)

    @app.errorhandler(Exception)
    def unhandled(exc):
        app.logger.exception("Unhandled error")  # stack trace goes to logs only
        db.session.rollback()
        return respond(500)


# Used by `python main.py` (local dev) and by `wsgi.py` (gunicorn/production)
app = create_app()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=debug)
