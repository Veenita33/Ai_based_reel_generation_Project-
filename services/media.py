"""
Media validation and probing for uploaded videos, images and audio.

Nothing here trusts the browser: the file *extension* picks the media kind, then the
file's actual bytes (magic numbers) must agree with it, images are decoded with Pillow,
and audio/video are probed with ffmpeg (from the `imageio-ffmpeg` package, so no system
install is needed). A renamed .exe or a text file called video.mp4 is rejected.

This module deliberately does not import Flask, so it is easy to unit-test.
"""
import logging
import os
import re
import subprocess
import warnings
from dataclasses import dataclass

from config import Config

log = logging.getLogger(__name__)

VIDEO, IMAGE, AUDIO = "video", "image", "audio"

MIME_BY_EXT = {
    "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/x-msvideo",
    "mkv": "video/x-matroska", "webm": "video/webm",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg",
}

# Which container/format signatures are acceptable for each extension.
_FAMILIES = {
    "mp4": {"ftyp"}, "mov": {"ftyp"}, "m4a": {"ftyp"}, "avi": {"avi"},
    "mkv": {"ebml"}, "webm": {"ebml"},
    "mp3": {"mp3"}, "wav": {"wav"}, "aac": {"adts", "ftyp"}, "ogg": {"ogg"},
    "jpg": {"jpeg"}, "jpeg": {"jpeg"}, "png": {"png"}, "webp": {"webp"},
}

FRIENDLY_TYPES = "MP4, MOV, AVI, MKV, WebM, JPG, PNG, WebP, MP3, WAV, M4A, AAC or OGG"


class MediaError(Exception):
    """`message` is safe to show to the user; `code` is stable for the frontend."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class MediaInfo:
    kind: str
    mime: str
    ext: str
    size: int
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None


# ----------------------------------------------------------------------------
# Kind / signature detection
# ----------------------------------------------------------------------------

def extension_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""


def kind_for_extension(ext, cfg=Config):
    if ext in cfg.ALLOWED_VIDEO_EXTENSIONS:
        return VIDEO
    if ext in cfg.ALLOWED_IMAGE_EXTENSIONS:
        return IMAGE
    if ext in cfg.ALLOWED_AUDIO_EXTENSIONS:
        return AUDIO
    return None


def sniff_family(head):
    """Identify a format from its first bytes. Returns a family name or None."""
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and len(head) >= 12:
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(head[8:12])
    if head[4:8] == b"ftyp":
        return "ftyp"                       # mp4 / mov / m4a
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "ebml"                       # matroska / webm
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "adts" if (head[1] & 0x06) == 0 else "mp3"   # layer bits 00 => AAC (ADTS)
    return None


def size_limit_bytes(kind, cfg=Config):
    mb = {VIDEO: cfg.MAX_VIDEO_MB, IMAGE: cfg.MAX_IMAGE_MB, AUDIO: cfg.MAX_AUDIO_MB}[kind]
    return mb * 1024 * 1024


# ----------------------------------------------------------------------------
# ffmpeg helpers
# ----------------------------------------------------------------------------

def ffmpeg_exe():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_SIZE_RE = re.compile(r"[ ,](\d{2,5})x(\d{2,5})[ ,\[]")


def probe_media(path, timeout=30):
    """
    Read container info by parsing `ffmpeg -i` output (ffprobe is not bundled).
    Returns {} when the file cannot be read at all, else
    {duration, has_video, has_audio, width, height}.
    """
    try:
        proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", path],
                              capture_output=True, text=True, timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError):
        log.warning("ffmpeg probe failed to run for %s", os.path.basename(path), exc_info=True)
        return {}
    text = proc.stderr or ""
    m = _DURATION_RE.search(text)
    info = {"duration": None, "has_video": False, "has_audio": False, "width": None, "height": None}
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    for line in text.splitlines():
        if "Stream #" not in line:
            continue
        if " Video:" in line and "attached pic" not in line:
            info["has_video"] = True
            sm = _SIZE_RE.search(line)
            if sm and not info["width"]:
                info["width"], info["height"] = int(sm.group(1)), int(sm.group(2))
        elif " Audio:" in line:
            info["has_audio"] = True
    if info["duration"] is None and not (info["has_video"] or info["has_audio"]):
        return {}
    return info


def probe_duration(path):
    """Length of an audio/video file in seconds, or None if it can't be read."""
    return probe_media(path).get("duration")


def _run_ffmpeg(args, timeout):
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    if proc.returncode != 0:
        raise MediaError("ffmpeg_failed", "That file couldn't be processed.")
    return proc


def extract_audio(src, dst, max_seconds=1200, timeout=300):
    """Write the audio track of `src` as 16 kHz mono WAV (what speech analysis needs).
    Raises MediaError if there is no audio or ffmpeg fails."""
    try:
        _run_ffmpeg(["-i", src, "-vn", "-t", str(max_seconds), "-ac", "1", "-ar", "16000",
                     "-c:a", "pcm_s16le", dst], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError("audio_extract_failed", "Audio couldn't be extracted.") from exc
    if not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        raise MediaError("audio_extract_failed", "Audio couldn't be extracted.")
    return dst


def make_analysis_proxy(src, dst, height=480, max_seconds=900, timeout=420, with_audio=True):
    """A small H.264 copy of a video so big uploads to the AI stay fast.
    Keeps the audio track unless `with_audio` is False."""
    audio_args = ["-c:a", "aac", "-b:a", "64k", "-ac", "1"] if with_audio else ["-an"]
    try:
        _run_ffmpeg(["-i", src, "-t", str(max_seconds), "-vf", f"scale=w=-2:h={height}", "-r", "15",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
                     *audio_args, "-movflags", "+faststart", dst], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaError("proxy_failed", "Video couldn't be prepared for analysis.") from exc
    if not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        raise MediaError("proxy_failed", "Video couldn't be prepared for analysis.")
    return dst


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------

def validate_file(path, original_name, cfg=Config):
    """
    Validate an already-saved upload. Returns MediaInfo or raises MediaError.
    Order: extension -> size -> magic bytes -> deep check (Pillow / ffmpeg).
    """
    name = os.path.basename(original_name or "file")
    ext = extension_of(name)
    kind = kind_for_extension(ext, cfg)
    if kind is None:
        raise MediaError("unsupported_type", f"'{name}' isn't a supported file type (use {FRIENDLY_TYPES}).")

    size = os.path.getsize(path)
    if size == 0:
        raise MediaError("empty_file", f"'{name}' is empty.")
    limit = size_limit_bytes(kind, cfg)
    if size > limit:
        raise MediaError("file_too_large",
                         f"'{name}' is too large. {kind.capitalize()} files can be up to {limit // (1024 * 1024)} MB.")

    with open(path, "rb") as fh:
        family = sniff_family(fh.read(32))
    if family not in _FAMILIES[ext]:
        raise MediaError("unsupported_type", f"'{name}' doesn't look like a valid .{ext} file.")

    info = MediaInfo(kind=kind, mime=MIME_BY_EXT[ext], ext=ext, size=size)

    if kind == IMAGE:
        _check_image(path, name, info, cfg)
        return info

    probe = probe_media(path)
    if not probe or not probe.get("duration"):
        raise MediaError("unreadable_media", f"'{name}' couldn't be read. It may be corrupted - try re-exporting it.")
    if kind == VIDEO and not probe["has_video"]:
        raise MediaError("unreadable_media", f"'{name}' has no video track.")
    if kind == AUDIO and not probe["has_audio"]:
        raise MediaError("unreadable_media", f"'{name}' has no audio track.")
    info.duration = probe["duration"]
    info.has_audio = probe["has_audio"]
    info.width, info.height = probe["width"], probe["height"]
    return info


def _check_image(path, name, info, cfg):
    from PIL import Image

    old = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = cfg.MAX_IMAGE_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as img:
                info.width, info.height = img.size
                img.verify()               # checks the file structure without decoding it all
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError("image_too_large", f"'{name}' has too many pixels. Please use a smaller image.") from exc
    except Exception as exc:  # noqa: BLE001 - Pillow raises many different errors for bad files
        raise MediaError("unreadable_media", f"'{name}' isn't a readable image.") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = old
    if not info.width or not info.height:
        raise MediaError("unreadable_media", f"'{name}' isn't a readable image.")


def precheck_upload(stream, filename, cfg=Config):
    """
    Cheap early check on an upload *stream*, before anything is written to disk: the extension
    is allowed, the size is within the limit for its kind, and the first bytes match the
    extension. Rewinds the stream and returns (kind, size). Raises MediaError.
    """
    name = os.path.basename(filename or "file")
    ext = extension_of(name)
    kind = kind_for_extension(ext, cfg)
    if kind is None:
        raise MediaError("unsupported_type", f"'{name}' isn't a supported file type (use {FRIENDLY_TYPES}).")
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        raise MediaError("empty_file", f"'{name}' is empty.")
    limit = size_limit_bytes(kind, cfg)
    if size > limit:
        raise MediaError("file_too_large",
                         f"'{name}' is too large. {kind.capitalize()} files can be up to {limit // (1024 * 1024)} MB.")
    head = stream.read(32)
    stream.seek(0)
    if sniff_family(head) not in _FAMILIES[ext]:
        raise MediaError("unsupported_type", f"'{name}' doesn't look like a valid .{ext} file.")
    return kind, size
