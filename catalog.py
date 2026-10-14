"""
Central catalogue of reel formats and narration voices.

Everything the UI shows (names, aspect ratios, resolutions) and everything the
render pipeline uses (width / height) comes from PLATFORM_FORMATS below, so a
platform can be added or changed in exactly one place.

`recommended_seconds` is only a *hint* shown in the UI - nothing in the
pipeline enforces it, because platform limits change over time.
"""
from dataclasses import dataclass
from math import gcd

MIN_DIMENSION = 360
MAX_DIMENSION = 2160


@dataclass(frozen=True)
class ReelFormat:
    key: str
    label: str            # "Instagram Reels"
    short_label: str      # "Instagram"
    icon: str             # Font Awesome class
    aspect_ratio: str     # "9:16"
    width: int
    height: int
    recommended_seconds: int | None
    customizable: bool = False

    @property
    def orientation(self):
        return orientation_for(self.width, self.height)

    @property
    def resolution(self):
        return f"{self.width} × {self.height}"

    def to_public(self):
        return {
            "key": self.key,
            "label": self.label,
            "short_label": self.short_label,
            "icon": self.icon,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "orientation": self.orientation,
            "recommended_seconds": self.recommended_seconds,
            "customizable": self.customizable,
        }


PLATFORM_FORMATS = {
    "instagram_reel": ReelFormat("instagram_reel", "Instagram Reels", "Instagram",
                                 "fa-brands fa-instagram", "9:16", 1080, 1920, 60),
    "youtube_short": ReelFormat("youtube_short", "YouTube Shorts", "YouTube",
                                "fa-brands fa-youtube", "9:16", 1080, 1920, 60),
    "tiktok": ReelFormat("tiktok", "TikTok", "TikTok",
                         "fa-brands fa-tiktok", "9:16", 1080, 1920, 60),
    "facebook_reel": ReelFormat("facebook_reel", "Facebook Reels", "Facebook",
                                "fa-brands fa-facebook", "9:16", 1080, 1920, 60),
    "custom": ReelFormat("custom", "Custom", "Custom",
                         "fa-solid fa-sliders", "9:16", 1080, 1920, None, customizable=True),
}
DEFAULT_FORMAT_KEY = "instagram_reel"

# Quick starting points offered in the "Custom" panel.
CUSTOM_PRESETS = [
    {"key": "vertical", "label": "Vertical", "aspect_ratio": "9:16", "width": 1080, "height": 1920},
    {"key": "portrait", "label": "Portrait", "aspect_ratio": "4:5", "width": 1080, "height": 1350},
    {"key": "square", "label": "Square", "aspect_ratio": "1:1", "width": 1080, "height": 1080},
    {"key": "landscape", "label": "Landscape", "aspect_ratio": "16:9", "width": 1920, "height": 1080},
]


def orientation_for(width, height):
    if width == height:
        return "Square"
    return "Vertical" if height > width else "Landscape"


def aspect_ratio_for(width, height):
    d = gcd(width, height) or 1
    return f"{width // d}:{height // d}"


def get_format(key):
    """Look up a format by key. Unknown/legacy keys fall back to Custom."""
    return PLATFORM_FORMATS.get(key) or PLATFORM_FORMATS["custom"]


def format_choices():
    return [f.to_public() for f in PLATFORM_FORMATS.values()]


@dataclass(frozen=True)
class ResolvedFormat:
    key: str
    aspect_ratio: str
    width: int
    height: int


def resolve_format(platform_key, width=None, height=None):
    """
    Validate a platform choice coming from the client and return the exact
    dimensions to render. The client's width/height are only honoured for the
    Custom format; for every other platform the server's own numbers win.
    Raises ValueError with a user-presentable message.
    """
    fmt = PLATFORM_FORMATS.get((platform_key or "").strip())
    if fmt is None:
        raise ValueError("Please choose where you want to publish this reel.")

    if not fmt.customizable:
        return ResolvedFormat(fmt.key, fmt.aspect_ratio, fmt.width, fmt.height)

    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("Enter a width and height for your custom format.") from None
    if not (MIN_DIMENSION <= w <= MAX_DIMENSION and MIN_DIMENSION <= h <= MAX_DIMENSION):
        raise ValueError(
            f"Custom width and height must be between {MIN_DIMENSION} and {MAX_DIMENSION} pixels."
        )
    w -= w % 2  # H.264 needs even dimensions
    h -= h % 2
    return ResolvedFormat(fmt.key, aspect_ratio_for(w, h), w, h)


# --------------------------------------------------------------------------
# Voices
# --------------------------------------------------------------------------
# ElevenLabs premade voices. Override or extend with the ELEVENLABS_VOICES env
# var:  "Name:voice_id,Name:voice_id"  (see .env.example).
_BUILTIN_VOICES = [
    ("21m00Tcm4TlvDq8ikWAM", "Rachel", "Calm, clear female"),
    ("pNInz6obpgDQGcFmaJgB", "Adam", "Deep, steady male"),
    ("EXAVITQu4vr4xnSDxMGB", "Bella", "Soft, friendly female"),
    ("ErXwobaYiN019PkySvjV", "Antoni", "Warm, well-rounded male"),
    ("TxGEqnHWrfWFTfGW9XjX", "Josh", "Deep, narrative male"),
]


def get_voices(config):
    """Return [{'id','name','description'}], default voice first."""
    voices = []
    raw = (config.get("ELEVENLABS_VOICES") or "").strip()
    if raw:
        for part in raw.split(","):
            name, _, vid = part.partition(":")
            if name.strip() and vid.strip():
                voices.append({"id": vid.strip(), "name": name.strip(), "description": ""})
    if not voices:
        voices = [{"id": i, "name": n, "description": d} for i, n, d in _BUILTIN_VOICES]

    default_id = config.get("ELEVENLABS_VOICE_ID")
    if default_id:
        existing = next((v for v in voices if v["id"] == default_id), None)
        if existing:
            voices.remove(existing)
            voices.insert(0, existing)
        else:
            voices.insert(0, {"id": default_id, "name": "Default voice", "description": "Configured on the server"})
    return voices


def find_voice(config, voice_id):
    return next((v for v in get_voices(config) if v["id"] == voice_id), None)


# --------------------------------------------------------------------------
# AI narration options (Gemini script writer)
# --------------------------------------------------------------------------
# To add a language: add one row to NARRATION_LANGUAGES. `gemini_name` is what the
# model is told; `wpm_factor` scales speaking speed for languages whose words are
# longer/shorter than English (Telugu words are longer, so fewer fit per minute).
NARRATION_LANGUAGES = {
    "en": {"label": "English", "gemini_name": "English", "wpm_factor": 1.0},
    "te": {"label": "Telugu (తెలుగు)", "gemini_name": "Telugu, written in Telugu script", "wpm_factor": 0.8},
    "hi": {"label": "Hindi (हिन्दी)", "gemini_name": "Hindi, written in Devanagari script", "wpm_factor": 0.95},
}
DEFAULT_NARRATION_LANGUAGE = "en"

NARRATION_DURATION_PRESETS = [15, 30, 45, 60, 90, 120]
DEFAULT_NARRATION_SECONDS = 60
MIN_NARRATION_SECONDS = 5

# Words per minute for English at each speed (low, high), as specified for the feature.
SPEAKING_SPEEDS = {
    "slow": {"label": "Slow", "wpm": (100, 120)},
    "normal": {"label": "Normal", "wpm": (130, 160)},
    "fast": {"label": "Fast", "wpm": (160, 180)},
}
DEFAULT_SPEED = "normal"

# Small nudge: lively tones are spoken a bit faster, reflective ones a bit slower.
_TONE_WPM_FACTOR = {"energetic": 1.05, "promotional": 1.04, "motivational": 1.02,
                    "cinematic": 0.92, "storytelling": 0.95, "emotional": 0.92, "documentary": 0.96}

NARRATION_TONES = {
    "professional": "Professional", "friendly": "Friendly", "energetic": "Energetic",
    "cinematic": "Cinematic", "storytelling": "Storytelling", "educational": "Educational",
    "promotional": "Promotional", "emotional": "Emotional", "casual": "Casual",
    "documentary": "Documentary", "motivational": "Motivational",
}
DEFAULT_TONE = "friendly"

NARRATION_STYLES = {
    "storytelling": "Storytelling", "explainer": "Explainer", "descriptive": "Descriptive",
    "commentary": "Commentary", "product_showcase": "Product showcase", "tutorial": "Step-by-step tutorial",
    "vlog": "Personal vlog", "news": "News-style report",
}
DEFAULT_STYLE = "storytelling"

NARRATION_AUDIENCES = {
    "general": "General audience", "students": "Students", "professionals": "Professionals",
    "customers": "Customers", "social": "Social media audience", "children": "Children",
    "travelers": "Travelers", "fitness": "Fitness audience", "food": "Food audience",
}
DEFAULT_AUDIENCE = "general"

IMAGE_FIT_MODES = {
    "crop": "Crop - fill the frame, trim the edges",
    "fit": "Fit - whole image, black bars",
    "fill": "Fill - whole image, blurred background",
}
DEFAULT_IMAGE_FIT = "crop"
MIN_IMAGE_SECONDS, MAX_IMAGE_SECONDS = 1, 15


def _wpm_range(speed, language, tone):
    lo, hi = SPEAKING_SPEEDS.get(speed, SPEAKING_SPEEDS[DEFAULT_SPEED])["wpm"]
    factor = NARRATION_LANGUAGES.get(language, NARRATION_LANGUAGES["en"])["wpm_factor"] * _TONE_WPM_FACTOR.get(tone, 1.0)
    return lo * factor, hi * factor


def target_word_range(seconds, speed=DEFAULT_SPEED, language="en", tone=None):
    """(min_words, max_words) for a narration of `seconds` - e.g. 60s/normal/English -> (130, 160)."""
    lo, hi = _wpm_range(speed, language, tone)
    return max(1, round(seconds / 60 * lo)), max(2, round(seconds / 60 * hi))


def estimate_seconds(word_count, speed=DEFAULT_SPEED, language="en", tone=None):
    lo, hi = _wpm_range(speed, language, tone)
    return word_count / ((lo + hi) / 2) * 60


def narration_client_config():
    """Everything create.js needs to estimate spoken length without a server round trip."""
    cfg = {"speeds": {}, "languages": {}, "tones": _TONE_WPM_FACTOR}
    for key, v in SPEAKING_SPEEDS.items():
        cfg["speeds"][key] = list(v["wpm"])
    for key, v in NARRATION_LANGUAGES.items():
        cfg["languages"][key] = v["wpm_factor"]
    return cfg


def _clean_text(value, limit):
    text = "".join(ch for ch in str(value or "") if ch == "\n" or ch >= " ")
    return text.strip()[:limit]


def resolve_narration_settings(data, max_seconds=180):
    """
    Validate narration settings sent by the browser (any mapping with .get).
    Returns (clean, errors). Unknown keys never reach the prompt: every choice is
    looked up in the tables above, only the free-text fields are passed through
    (length-limited, control characters removed).
    """
    errors, clean = {}, {}

    try:
        seconds = int(float(data.get("duration_seconds") or DEFAULT_NARRATION_SECONDS))
    except (TypeError, ValueError):
        seconds = None
    if seconds is None or not (MIN_NARRATION_SECONDS <= seconds <= max_seconds):
        errors["duration_seconds"] = f"Narration length must be between {MIN_NARRATION_SECONDS} and {max_seconds} seconds."
        seconds = DEFAULT_NARRATION_SECONDS
    clean["duration_seconds"] = seconds

    def choice(field, table, default, label):
        value = (data.get(field) or default)
        if value not in table:
            errors[field] = f"Please choose a valid {label}."
            value = default
        clean[field] = value

    choice("language", NARRATION_LANGUAGES, DEFAULT_NARRATION_LANGUAGE, "language")
    choice("tone", NARRATION_TONES, DEFAULT_TONE, "tone")
    choice("style", NARRATION_STYLES, DEFAULT_STYLE, "style")
    choice("speed", SPEAKING_SPEEDS, DEFAULT_SPEED, "speaking speed")

    audience = data.get("audience") or DEFAULT_AUDIENCE
    if audience == "custom":
        audience_text = _clean_text(data.get("audience_custom"), 100)
        if not audience_text:
            errors["audience"] = "Describe your target audience."
        clean["audience"], clean["audience_label"] = "custom", audience_text
    elif audience in NARRATION_AUDIENCES:
        clean["audience"], clean["audience_label"] = audience, NARRATION_AUDIENCES[audience]
    else:
        errors["audience"] = "Please choose a valid audience."
        clean["audience"], clean["audience_label"] = DEFAULT_AUDIENCE, NARRATION_AUDIENCES[DEFAULT_AUDIENCE]

    clean["tone_custom"] = _clean_text(data.get("tone_custom"), 150)
    clean["instructions"] = _clean_text(data.get("instructions"), 1000)
    return clean, errors
