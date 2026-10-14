"""
Thin wrapper around the ElevenLabs text-to-speech REST API.

To change the API key: set the ELEVENLABS_API_KEY environment variable
(in your local .env file, or in your hosting provider's dashboard).
Nothing in this file needs to be edited to change keys/voices - see
ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL_ID in config.py.
"""
import requests

ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class ElevenLabsError(Exception):
    """`str(exc)` is the technical detail (log it); `user_message` is safe to show."""

    DEFAULT_USER_MESSAGE = "We couldn't generate the voiceover right now. Please try again in a few minutes."

    def __init__(self, message, user_message=None):
        super().__init__(message)
        self.user_message = user_message or self.DEFAULT_USER_MESSAGE


def _error_status(response):
    """(status, detail) strings from an ElevenLabs error body, lower-cased; ("", "") if unparseable."""
    try:
        body = response.json()
    except ValueError:
        return "", ""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("status", "")).lower(), str(detail.get("message", "")).lower()
    return "", str(detail or "").lower()


def generate_voiceover(text, output_path, api_key, voice_id, model_id="eleven_multilingual_v2"):
    """
    Calls the ElevenLabs API and writes an MP3 of the spoken `text` to
    `output_path`. Raises ElevenLabsError with a human-readable message
    on failure (bad/missing key, rate limit, etc).
    """
    if not api_key:
        raise ElevenLabsError(
            "No ElevenLabs API key is configured. Set ELEVENLABS_API_KEY "
            "in your .env file (local) or in your host's environment "
            "variables (Render/Vercel dashboard), then restart the app.",
            user_message="Voiceover is temporarily unavailable. Please try again later.",
        )
    if not text or not text.strip():
        raise ElevenLabsError("No narration text was provided.",
                              user_message="Please enter the narration you want spoken.")

    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text.strip(),
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
    except requests.RequestException as exc:
        raise ElevenLabsError(
            f"Could not reach ElevenLabs: {exc}",
            user_message="The voiceover service took too long to respond. Please try again.",
        ) from exc

    status, detail = ("", "") if response.status_code == 200 else _error_status(response)
    if response.status_code == 200:
        pass
    elif "quota" in status or "quota" in detail:
        raise ElevenLabsError("ElevenLabs quota exceeded.",
                              user_message="The voiceover service has reached its usage limit. Please try again later.")
    elif response.status_code == 404 or "voice_not_found" in status:
        raise ElevenLabsError(f"ElevenLabs voice not found: {voice_id}",
                              user_message="That voice isn't available any more. Please pick a different voice.")
    elif response.status_code == 401:
        raise ElevenLabsError("ElevenLabs rejected the request: invalid API key.",
                              user_message="Voiceover is temporarily unavailable. Please try again later.")
    elif response.status_code == 429:
        raise ElevenLabsError("ElevenLabs rate limit / concurrency exceeded. Try again later.",
                              user_message="Our voiceover service is busy right now. Please try again in a little while.")
    else:
        raise ElevenLabsError(
            f"ElevenLabs API error ({response.status_code}): {response.text[:300]}"
        )

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path
