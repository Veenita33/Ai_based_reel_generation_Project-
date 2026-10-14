import json
import uuid
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from extensions import db


def utcnow():
    return datetime.utcnow()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    # Billing-ready hook: see PLANS in services/quota.py. Everyone is "free" today.
    plan = db.Column(db.String(20), nullable=False, default="free", server_default="free")

    # One user -> many reels. A reel belongs to exactly one user (or, for a
    # guest's free reel, to nobody yet - see Reel.guest_token).
    reels = db.relationship("Reel", backref="creator", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Reel(db.Model):
    __tablename__ = "reels"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Ownership: exactly one of these identifies the owner.
    #   user_id      -> a registered user's reel
    #   guest_token  -> a free-trial reel made before signing up (user_id is NULL)
    # When the guest signs in/registers the reel is claimed: user_id is set and
    # guest_token cleared.
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    guest_token = db.Column(db.String(64), nullable=True, index=True)

    title = db.Column(db.String(200), default="Untitled Reel")
    script_text = db.Column(db.Text, default="")
    song = db.Column(db.String(120), nullable=True)
    voice_id = db.Column(db.String(64), nullable=True)
    voice_name = db.Column(db.String(80), nullable=True)

    # Format chosen at creation time (keys come from catalog.PLATFORM_FORMATS).
    reel_format = db.Column(db.String(40), nullable=True)
    aspect_ratio = db.Column(db.String(16), nullable=True)
    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    duration = db.Column(db.Float, nullable=True)  # seconds

    # Files, as paths RELATIVE to STORAGE_ROOT. Never exposed to clients.
    video_path = db.Column(db.String(400), nullable=True)
    thumbnail_path = db.Column(db.String(400), nullable=True)
    filename = db.Column(db.String(255), nullable=True)   # legacy: basename of the mp4
    thumbnail = db.Column(db.String(255), nullable=True)  # legacy

    # AI narration metadata (all nullable: reels made before this feature simply have none).
    # The script itself is script_text above.
    narration_status = db.Column(db.String(20), nullable=True)   # "manual" | "ai" | "ai_edited"
    narration_language = db.Column(db.String(16), nullable=True)
    narration_tone = db.Column(db.String(40), nullable=True)
    narration_style = db.Column(db.String(40), nullable=True)
    narration_duration_requested = db.Column(db.Integer, nullable=True)   # seconds the user asked for
    narration_duration_actual = db.Column(db.Float, nullable=True)        # seconds of generated voiceover
    gemini_analysis_status = db.Column(db.String(20), nullable=True)      # "ok" | "partial"
    media_count = db.Column(db.Integer, nullable=True)
    media_types = db.Column(db.String(60), nullable=True)                 # e.g. "video,image"
    draft_id = db.Column(db.String(36), nullable=True)                    # MediaDraft the reel was made from
    generated_at = db.Column(db.DateTime, nullable=True)

    # pending -> processing -> done / failed ;  stage = what the worker is doing now
    status = db.Column(db.String(20), default="pending")
    stage = db.Column(db.String(30), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def resolution(self):
        return f"{self.width} × {self.height}" if self.width and self.height else None

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "error_message": self.error_message,
        }


class GuestUsage(db.Model):
    """
    One row per free-trial generation by a not-signed-in visitor.

    Kept in its own table (not derived from `reels`) so deleting the reel does
    NOT hand the visitor another free one. Matching is by signed-cookie token
    and, optionally, a salted hash of the IP address.
    """
    __tablename__ = "guest_usage"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, index=True)
    ip_hash = db.Column(db.String(64), nullable=True, index=True)
    reel_id = db.Column(db.String(36), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow)


class MediaDraft(db.Model):
    """
    Uploaded media + the AI-written narration, kept between "Generate AI Narration" and
    "Generate reel" so the user can edit/regenerate without uploading everything again.

    Same ownership rules as Reel: a signed-in user's draft has user_id; a guest's has
    guest_token. Files live under STORAGE_ROOT/drafts/<id>/ (private, never in static/) and
    are removed after DRAFT_TTL_HOURS.
    """
    __tablename__ = "media_drafts"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    guest_token = db.Column(db.String(64), nullable=True, index=True)

    # pending -> processing -> ready / failed ; stage = analyzing | understanding | writing | ready
    status = db.Column(db.String(20), default="pending")
    stage = db.Column(db.String(30), nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    media_json = db.Column(db.Text, default="[]")       # ordered manifest, see services/drafts.py
    settings_json = db.Column(db.Text, default="{}")    # validated narration settings
    script_text = db.Column(db.Text, default="")
    analysis_status = db.Column(db.String(20), nullable=True)   # "ok" | "partial"
    notes_json = db.Column(db.Text, default="[]")       # user-facing notes ("audio could not be analyzed")
    runs = db.Column(db.Integer, default=0)             # Gemini runs spent on this draft (capped)

    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    @property
    def media(self):
        return json.loads(self.media_json or "[]")

    @property
    def settings(self):
        return json.loads(self.settings_json or "{}")

    @property
    def notes(self):
        return json.loads(self.notes_json or "[]")
