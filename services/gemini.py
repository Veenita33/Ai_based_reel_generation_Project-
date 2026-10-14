"""
Gemini integration: looks at the uploaded videos / images / audio (in the order the
creator arranged them) and writes a spoken narration script for ElevenLabs to voice.

Uses the official `google-genai` SDK (`pip install google-genai`). The API key comes
from the GEMINI_API_KEY environment variable (see config.py) and is never sent to the
browser. Technical failures are logged by the caller; every GeminiError carries a
`user_message` that is safe to show in the UI.

This module does not import Flask, so it can be tested on its own.
"""
import logging
import os
import re
import time

import catalog
from services import media as media_svc

log = logging.getLogger(__name__)


class GeminiError(Exception):
    """`str(exc)` = technical detail (log it). `user_message` = safe for the UI.
    `fatal` errors (bad key, quota...) affect every file, so we stop instead of skipping."""

    MESSAGES = {
        "not_configured": "AI narration isn't set up on this server yet. You can still write your narration by hand.",
        "invalid_key": "AI narration is temporarily unavailable. Please try again later.",
        "quota": "The AI service is busy right now (usage limit reached). Please try again in a few minutes.",
        "timeout": "Creating the narration took too long. Try fewer or shorter files, or try again.",
        "unavailable": "AI narration is temporarily unavailable. Please try again.",
        "no_media": "We couldn't analyze any of your files. Try different files, or write the narration by hand.",
        "blocked": "The AI couldn't write a narration for this content. Try different media or change your instructions.",
        "empty": "The AI didn't return a narration. Please try again.",
        "failed": "AI narration is temporarily unavailable. Please try again.",
    }
    FATAL = {"not_configured", "invalid_key", "quota", "timeout", "unavailable", "blocked"}

    def __init__(self, code, detail=""):
        super().__init__(detail or code)
        self.code = code
        self.user_message = self.MESSAGES.get(code, self.MESSAGES["failed"])
        self.fatal = code in self.FATAL


class NarrationResult:
    def __init__(self, script, analysis_status, notes):
        self.script = script
        self.analysis_status = analysis_status      # "ok" | "partial"
        self.notes = notes                          # user-facing sentences, e.g. "Audio in x.mp4 couldn't be analyzed."
        self.word_count = count_words(script)


def count_words(text):
    return len((text or "").split())


# ----------------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------------

PROMPT_HEAD = """You are an expert social-media video scriptwriter.

Analyze the provided media carefully.

Create a narration based ONLY on information supported by the uploaded media and the user's instructions.
Do not invent specific facts (names, prices, places, numbers, claims) that cannot be inferred from the media.
Follow the chronological order of the media: the creator arranged the items in the order they should appear in the reel.
The narration must sound natural when spoken aloud, like a person talking, not like written text.

What to look at:
- Images: objects, people, environment, visible text, products, actions, important details, context.
- Videos: major scenes, actions, objects, people, environment, transitions, visible text, the overall story, and any speech or sounds in the audio track, in timeline order.
- Audio files: spoken content, important statements, topics, and relevant sound context.
If the media contains useful speech, build on it (do not ignore it and do not simply transcribe it)."""

PROMPT_RULES = """Create a coherent beginning, middle and ending where appropriate.
Avoid unnecessary repetition.
Avoid describing every frame literally; focus on the most meaningful information.
The output must be ready for text-to-speech.

Do not include:
- headings
- bullet points
- markdown
- stage directions
- camera directions
- explanations
- quotation marks around the whole script

Return only the narration script."""


def build_prompt(settings, media, notes=None):
    """The text instructions sent to Gemini together with the media files."""
    lang = catalog.NARRATION_LANGUAGES[settings["language"]]
    seconds = settings["duration_seconds"]
    lo, hi = catalog.target_word_range(seconds, settings["speed"], settings["language"], settings["tone"])
    tone = catalog.NARRATION_TONES[settings["tone"]]
    if settings.get("tone_custom"):
        tone += f" (also: {settings['tone_custom']})"
    style = catalog.NARRATION_STYLES[settings["style"]]
    speed = catalog.SPEAKING_SPEEDS[settings["speed"]]["label"].lower()

    lines = [PROMPT_HEAD, "", "Media order:"]
    for i, item in enumerate(media, 1):
        lines.append(f"{i}. {item['kind'].capitalize()}: {item['name']}")
    lines += [
        "",
        f"Target duration: {seconds} seconds when spoken at a {speed} pace.",
        f"Length: write between {lo} and {hi} words in total. Stay inside this range, because the voiceover "
        f"has to fit {seconds} seconds.",
        f"Language: {lang['gemini_name']}. Write naturally in this language for spoken audio. "
        "Do not translate word-for-word from another language.",
        f"Tone: {tone}.",
        f"Style: {style}.",
        f"Audience: {settings['audience_label']}.",
        f"Speaking speed: {speed}.",
    ]
    if settings.get("instructions"):
        lines += ["",
                  "Additional instructions from the creator (follow them unless they conflict with the "
                  "output rules below):",
                  "<<<", settings["instructions"], ">>>"]
    if notes:
        lines += ["", "Note about the media: " + " ".join(notes)]
    lines += ["", PROMPT_RULES]
    return "\n".join(lines)


def build_adjust_prompt(script, settings, target_words, seconds, measured_seconds=None):
    lo, hi = target_words
    lang = catalog.NARRATION_LANGUAGES[settings["language"]]
    why = ""
    if measured_seconds:
        why = (f"When voiced, the current version lasts about {measured_seconds:.0f} seconds, "
               f"but it needs to last about {seconds} seconds. ")
    return "\n".join([
        "You are an expert social-media video scriptwriter.",
        f"Rewrite the narration below so that it is between {lo} and {hi} words, to fill about {seconds} seconds "
        "of spoken audio. " + why + "Keep the same facts, order, language, tone and meaning. "
        "Add or trim detail naturally; do not repeat yourself and do not invent new specific facts.",
        f"Language: {lang['gemini_name']}.",
        f"Tone: {catalog.NARRATION_TONES[settings['tone']]}.",
        "",
        "It must be ready for text-to-speech: no headings, bullet points, markdown, stage directions, "
        "explanations, or quotation marks around the whole script. Return only the narration script.",
        "",
        "Current narration:",
        "<<<", script, ">>>",
    ])


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LABEL = re.compile(r"^\s*(?:narration|script|voice[- ]?over)\s*:\s*", re.I)


def clean_script(text):
    """Strip markdown, labels and stage directions the model sometimes adds anyway."""
    text = (text or "").strip()
    text = _FENCE.sub("", text).strip()
    text = _LABEL.sub("", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)         # headings
    text = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", "", text, flags=re.M)  # bullets / numbering
    text = re.sub(r"(\*\*|__|\*|`)", "", text)                          # emphasis / code marks
    text = re.sub(r"^\s*\[[^\]\n]{1,80}\]\s*$", "", text, flags=re.M)   # whole-line [Music fades in]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # unwrap quotes only when they wrap the WHOLE script (not when the text just contains quotes)
    if len(text) > 2 and ((text[0] == '"' and text[-1] == '"' and text.count('"') == 2)
                          or (text[0] == "\u201c" and text[-1] == "\u201d"
                              and text.count("\u201c") == 1 and text.count("\u201d") == 1)):
        text = text[1:-1].strip()
    return text


# ----------------------------------------------------------------------------
# SDK plumbing
# ----------------------------------------------------------------------------

def _sdk():
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise GeminiError("not_configured", "google-genai is not installed") from exc
    return genai, types


def _client(api_key, timeout_seconds):
    if not api_key:
        raise GeminiError("not_configured", "GEMINI_API_KEY is not set")
    genai, types = _sdk()
    try:
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)))
    except (TypeError, ValueError):
        return genai.Client(api_key=api_key)


def map_error(exc):
    """Translate any SDK / network exception into a GeminiError."""
    if isinstance(exc, GeminiError):
        return exc
    detail = f"{type(exc).__name__}: {exc}"
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    if isinstance(code, int):
        if code == 429 or "resource_exhausted" in text or "quota" in text:
            return GeminiError("quota", detail)
        if code in (401, 403) or "api key not valid" in text or "api_key_invalid" in text or "permission_denied" in text:
            return GeminiError("invalid_key", detail)
        if code == 400 and "api key" in text:
            return GeminiError("invalid_key", detail)
        if code in (408, 504) or "deadline" in text:
            return GeminiError("timeout", detail)
        if code >= 500:
            return GeminiError("unavailable", detail)
        return GeminiError("failed", detail)
    if "timeout" in name or "timed out" in text or "deadline" in text:
        return GeminiError("timeout", detail)
    if "connect" in name or "network" in name or "connection" in text:
        return GeminiError("unavailable", detail)
    return GeminiError("failed", detail)


def _state_name(f):
    state = getattr(f, "state", None)
    return str(getattr(state, "name", state) or "").upper()


def _upload_and_wait(client, path, mime, deadline):
    """Upload through the Files API and wait until Gemini has finished processing it."""
    _, types = _sdk()
    f = client.files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    while _state_name(f) == "PROCESSING":
        if time.monotonic() > deadline:
            raise GeminiError("timeout", "file processing exceeded the deadline")
        time.sleep(2)
        f = client.files.get(name=f.name)
    if _state_name(f) == "FAILED":
        raise GeminiError("failed", f"Gemini could not process {os.path.basename(path)}")
    return f


def _delete_quietly(client, files):
    for f in files:
        try:
            client.files.delete(name=f.name)
        except Exception:  # noqa: BLE001 - files auto-expire after 48h anyway
            log.debug("Could not delete Gemini file %s", getattr(f, "name", "?"), exc_info=True)


BIG_VIDEO_BYTES = 30 * 1024 * 1024   # larger videos go through a small proxy copy first


def _attempts(item, workdir):
    """
    Yield (label_note, path, mime) candidates to try for one media item, best first.
    The note (or None) is appended to the user-facing notes if that candidate is the one used.
    Videos degrade gracefully: original -> small copy -> small copy without audio -> audio only.
    """
    path, mime, name = item["path"], item["mime"], item["name"]
    if item["kind"] != media_svc.VIDEO:
        yield None, path, mime
        return

    big = os.path.getsize(path) > BIG_VIDEO_BYTES
    if not big:
        yield None, path, mime
    base = os.path.join(workdir, item["id"])
    try:
        yield None, media_svc.make_analysis_proxy(path, base + "_proxy.mp4"), "video/mp4"
    except media_svc.MediaError:
        pass
    if item.get("has_audio") is not False:
        try:
            yield (f"The audio in '{name}' couldn't be analyzed."), \
                media_svc.make_analysis_proxy(path, base + "_noaudio.mp4", with_audio=False), "video/mp4"
        except media_svc.MediaError:
            pass
        try:
            yield (f"Only the audio of '{name}' could be analyzed, not its pictures."), \
                media_svc.extract_audio(path, base + "_audio.wav"), "audio/wav"
        except media_svc.MediaError:
            pass


def _prepare_media(client, media, workdir, deadline):
    """Upload everything. Returns (parts, uploaded_files, notes). Skips what can't be analyzed."""
    parts, uploaded, notes = [], [], []
    total = len(media)
    for i, item in enumerate(media, 1):
        label = f"Media {i} of {total} - {item['kind']} - \"{item['name']}\":"
        done = False
        for note, path, mime in _attempts(item, workdir):
            try:
                f = _upload_and_wait(client, path, mime, deadline)
            except Exception as exc:  # noqa: BLE001
                err = map_error(exc)
                if err.fatal:
                    raise err from exc
                log.warning("Gemini could not use %s (%s): %s", item["name"], mime, err)
                continue
            uploaded.append(f)
            parts += [label, f]
            if note:
                notes.append(note)
            done = True
            break
        if not done:
            notes.append(f"'{item['name']}' couldn't be analyzed and was skipped.")
    if not uploaded:
        raise GeminiError("no_media", "none of the uploaded files could be analyzed")
    return parts, uploaded, notes


def _generate_text(client, model, contents, deadline, on_first_chunk=None, temperature=0.8):
    """Stream a response so the UI can move from 'understanding' to 'writing' for real."""
    _, types = _sdk()
    config = types.GenerateContentConfig(temperature=temperature, max_output_tokens=16384)
    pieces, first, feedback = [], True, None
    try:
        for chunk in client.models.generate_content_stream(model=model, contents=contents, config=config):
            if time.monotonic() > deadline:
                raise GeminiError("timeout", "generation exceeded the deadline")
            feedback = getattr(chunk, "prompt_feedback", None) or feedback
            text = getattr(chunk, "text", None)
            if text:
                if first and on_first_chunk:
                    on_first_chunk()
                first = False
                pieces.append(text)
    except GeminiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_error(exc) from exc
    out = clean_script("".join(pieces))
    if not out:
        if getattr(feedback, "block_reason", None):
            raise GeminiError("blocked", f"blocked: {feedback.block_reason}")
        raise GeminiError("empty", "model returned no text")
    return out


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------

def _in_range(words, lo, hi):
    return lo * 0.88 <= words <= hi * 1.12


def adjust_narration(api_key, model, script, settings, timeout=90, measured_seconds=None):
    """Text-only rewrite of an existing script to hit the requested duration."""
    client = _client(api_key, timeout)
    seconds = settings["duration_seconds"]
    words = count_words(script)
    if measured_seconds and words:
        mid = max(2, round(words * seconds / measured_seconds))
        target = (max(1, round(mid * 0.95)), max(2, round(mid * 1.05)))
    else:
        target = catalog.target_word_range(seconds, settings["speed"], settings["language"], settings["tone"])
    prompt = build_adjust_prompt(script, settings, target, seconds, measured_seconds)
    return _generate_text(client, model, [prompt], time.monotonic() + timeout, temperature=0.5)


def generate_narration(api_key, model, media, settings, workdir, on_stage=None, timeout=240):
    """
    media:    ordered list of {id, name, kind, mime, path, has_audio}
    settings: output of catalog.resolve_narration_settings()
    on_stage: callback with "analyzing" -> "understanding" -> "writing"
    Returns a NarrationResult. Raises GeminiError.
    """
    stage = on_stage or (lambda name: None)
    if not media:
        raise GeminiError("no_media", "no media supplied")
    client = _client(api_key, min(timeout, 120))
    deadline = time.monotonic() + timeout
    os.makedirs(workdir, exist_ok=True)

    stage("analyzing")
    uploaded = []
    try:
        parts, uploaded, notes = _prepare_media(client, media, workdir, deadline)
        stage("understanding")
        prompt = build_prompt(settings, media, notes)
        script = _generate_text(client, model, parts + [prompt], deadline,
                                on_first_chunk=lambda: stage("writing"))

        lo, hi = catalog.target_word_range(settings["duration_seconds"], settings["speed"],
                                           settings["language"], settings["tone"])
        words = count_words(script)
        if not _in_range(words, lo, hi):
            # One cheap text-only correction pass so the voiceover lands near the requested length.
            log.info("Narration has %d words, wanted %d-%d; adjusting once", words, lo, hi)
            try:
                better = _generate_text(
                    client, model,
                    [build_adjust_prompt(script, settings, (lo, hi), settings["duration_seconds"])],
                    deadline, temperature=0.5)
                if abs(count_words(better) - (lo + hi) / 2) < abs(words - (lo + hi) / 2):
                    script = better
            except GeminiError as exc:
                log.warning("Length adjustment skipped: %s", exc)
    except GeminiError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise map_error(exc) from exc
    finally:
        _delete_quietly(client, uploaded)

    return NarrationResult(script, "partial" if notes else "ok", notes)
