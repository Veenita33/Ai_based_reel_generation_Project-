import os
from dotenv import load_dotenv

# Load variables from a local .env file when present (used for local dev).
# On Render/Vercel/etc you set these in the host's dashboard instead, and
# load_dotenv() simply does nothing because there is no .env file there.
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    # --- Core Flask config -------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")

    # --- Database ------------------------------------------------------------
    # Defaults to a local SQLite file so the app works out of the box.
    # On Render you can instead set DATABASE_URL to a managed Postgres URL.
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'app.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- ElevenLabs (text -> voiceover) --------------------------------------
    ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
    # Default voice = "Rachel", a built-in ElevenLabs voice. Change via env var.
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
    ELEVENLABS_MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    ELEVENLABS_VOICES = os.environ.get("ELEVENLABS_VOICES", "")  # optional "Name:id,Name:id"

    # --- Gemini (media understanding + narration script) ------------------------
    # Never hardcode the key: set GEMINI_API_KEY in .env / your host's dashboard.
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    # Any multimodal Gemini model that accepts image + video + audio input.
    GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    # Hard ceiling for one narration run (upload + analysis + writing), in seconds.
    GEMINI_TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", 240))

    # --- Storage ---------------------------------------------------------------
    # Everything private lives OUTSIDE static/, so files are only reachable
    # through the authenticated routes in reels.py:
    #   <STORAGE_ROOT>/users/<user_id>/reels/<reel_id>.mp4   (+ .jpg thumbnail)
    #   <STORAGE_ROOT>/guests/reels/<reel_id>.mp4            (free-trial reels, until claimed)
    #   <STORAGE_ROOT>/tmp/<reel_id>/                         (raw uploads while rendering)
    STORAGE_ROOT = os.environ.get("REEL_STORAGE_DIR", os.path.join(BASE_DIR, "storage"))
    UPLOAD_FOLDER = os.path.join(STORAGE_ROOT, "tmp")
    SONGS_FOLDER = os.path.join(BASE_DIR, "static", "songs")
    LEGACY_REELS_FOLDER = os.path.join(BASE_DIR, "static", "reels")  # pre-upgrade location (migrated on boot)
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH_MB", 200)) * 1024 * 1024
    ALLOWED_VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
    ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
    ALLOWED_AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "ogg"}

    # Per-file limits (MB) on top of MAX_CONTENT_LENGTH, which caps the whole request.
    MAX_IMAGE_MB = int(os.environ.get("MAX_IMAGE_MB", 20))
    MAX_VIDEO_MB = int(os.environ.get("MAX_VIDEO_MB", 150))
    MAX_AUDIO_MB = int(os.environ.get("MAX_AUDIO_MB", 50))
    MAX_IMAGE_PIXELS = 100_000_000   # decompression-bomb guard

    # Default output size when nothing else is specified (Custom format default).
    REEL_WIDTH = 1080
    REEL_HEIGHT = 1920

    # --- Input limits -------------------------------------------------------------
    MAX_MEDIA_FILES = int(os.environ.get("MAX_MEDIA_FILES", 20))   # videos + images + audio, per reel
    MAX_CLIPS = MAX_MEDIA_FILES                                      # legacy name, same limit
    MAX_NARRATION_CHARS = 6000
    DEFAULT_IMAGE_SECONDS = 3
    MAX_NARRATION_SECONDS = 180
    NARRATION_TOLERANCE_SECONDS = 5      # voiceover vs requested length before we flag it
    # Narration drafts (each one costs a Gemini call): per owner, per hour.
    NARRATION_DRAFTS_PER_HOUR_USER = int(os.environ.get("NARRATION_DRAFTS_PER_HOUR_USER", 20))
    NARRATION_DRAFTS_PER_HOUR_GUEST = int(os.environ.get("NARRATION_DRAFTS_PER_HOUR_GUEST", 4))
    DRAFT_TTL_HOURS = int(os.environ.get("DRAFT_TTL_HOURS", 24))    # uploaded media kept for edits/re-renders
    REEL_STALE_MINUTES = int(os.environ.get("REEL_STALE_MINUTES", 30))

    # --- Free trial / quotas --------------------------------------------------------
    # Number of reels an anonymous visitor may create before they must sign in.
    GUEST_FREE_REELS = int(os.environ.get("GUEST_FREE_REELS", 1))
    # Also remember guests by (hashed) IP so clearing cookies / incognito does not
    # reset the trial. Set to 0 if many of your visitors share one public IP.
    GUEST_TRACK_BY_IP = os.environ.get("GUEST_TRACK_BY_IP", "1") == "1"
    # Honour X-Forwarded-For from your host's proxy (Render, Heroku, nginx...).
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") == "1"

    # --- Sessions / cookies ---------------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 90  # 90 days (keeps the guest trial marker)
