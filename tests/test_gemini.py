"""
Tests for the Gemini narration service and media validation. No API key, network or Flask needed:
the `google.genai` SDK is replaced by a small fake, media files are generated with the ffmpeg
bundled in imageio-ffmpeg.

    python -m unittest tests.test_gemini -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types as pytypes
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------------------------------------------------------------------------
# Fake google.genai
# ---------------------------------------------------------------------------
CALLS = {"uploads": [], "contents": [], "deleted": [], "fail_upload": None, "script": "", "errors": []}


class FakeAPIError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeFile:
    def __init__(self, name, state="ACTIVE"):
        self.name = name
        self.state = pytypes.SimpleNamespace(name=state)


class FakeFiles:
    def upload(self, file, config=None):
        CALLS["uploads"].append((os.path.basename(file), getattr(config, "mime_type", None)))
        if CALLS["fail_upload"] and CALLS["fail_upload"](file):
            raise FakeAPIError(400, "invalid argument: unsupported media")
        return FakeFile(f"files/{len(CALLS['uploads'])}")

    def get(self, name):
        return FakeFile(name)

    def delete(self, name):
        CALLS["deleted"].append(name)


class FakeModels:
    def generate_content_stream(self, model, contents, config=None):
        CALLS["contents"].append(contents)
        if CALLS["errors"]:
            raise CALLS["errors"].pop(0)
        script = CALLS["script"].pop(0) if isinstance(CALLS["script"], list) else CALLS["script"]
        half = len(script) // 2
        yield pytypes.SimpleNamespace(text=script[:half], prompt_feedback=None)
        yield pytypes.SimpleNamespace(text=script[half:], prompt_feedback=None)


class FakeClient:
    def __init__(self, api_key=None, http_options=None):
        self.api_key = api_key
        self.files, self.models = FakeFiles(), FakeModels()


fake_types = pytypes.ModuleType("google.genai.types")
fake_types.UploadFileConfig = lambda **kw: pytypes.SimpleNamespace(**kw)
fake_types.GenerateContentConfig = lambda **kw: pytypes.SimpleNamespace(**kw)
fake_types.HttpOptions = lambda **kw: pytypes.SimpleNamespace(**kw)
fake_genai = pytypes.ModuleType("google.genai")
fake_genai.Client, fake_genai.types = FakeClient, fake_types
fake_google = pytypes.ModuleType("google")
fake_google.genai = fake_genai
sys.modules.update({"google": fake_google, "google.genai": fake_genai, "google.genai.types": fake_types})

import catalog  # noqa: E402
from services import gemini, media  # noqa: E402

SETTINGS, _ = catalog.resolve_narration_settings({"duration_seconds": 30, "language": "en", "tone": "energetic",
                                                  "instructions": "Make it sound like a travel influencer."})
LO, HI = catalog.target_word_range(30, "normal", "en", "energetic")


def words(n):
    return " ".join(["word"] * n)


def ff(*args):
    subprocess.run([media.ffmpeg_exe(), "-loglevel", "error", "-y", *args], check=True)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gemtest_")
        t = cls.tmp
        ff("-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.path.join(t, "clip.mp4"))
        ff("-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=1", "-frames:v", "1", os.path.join(t, "pic.png"))
        ff("-f", "lavfi", "-i", "sine=duration=1", os.path.join(t, "voice.wav"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        CALLS.update(uploads=[], contents=[], deleted=[], fail_upload=None, errors=[], script=words((LO + HI) // 2))

    def items(self):
        t = self.tmp
        return [
            dict(id="m1", name="product.png", kind="image", mime="image/png", path=os.path.join(t, "pic.png")),
            dict(id="m2", name="demo.mp4", kind="video", mime="video/mp4", path=os.path.join(t, "clip.mp4"), has_audio=True),
            dict(id="m3", name="note.wav", kind="audio", mime="audio/wav", path=os.path.join(t, "voice.wav")),
        ]

    def run_gen(self, items=None, stages=None, **kw):
        return gemini.generate_narration("key", "gemini-test", items or self.items(), SETTINGS,
                                         os.path.join(self.tmp, "work"), on_stage=(stages.append if stages is not None else None), **kw)


class PromptAndOrder(Base):
    def test_media_order_and_settings_reach_gemini(self):
        stages = []
        res = self.run_gen(stages=stages)
        contents = CALLS["contents"][0]
        labels = [c for c in contents if isinstance(c, str) and c.startswith("Media ")]
        self.assertEqual([l.split(" - ")[1] for l in labels], ["image", "video", "audio"])      # order preserved
        prompt = contents[-1]
        for needle in ("30 seconds", f"between {LO} and {HI} words", "Tone: Energetic", "Audience: General audience",
                       "travel influencer", "1. Image: product.png", "2. Video: demo.mp4", "3. Audio: note.wav",
                       "Return only the narration script", "English"):
            self.assertIn(needle, prompt)
        self.assertEqual(stages, ["analyzing", "understanding", "writing"])
        self.assertEqual(res.analysis_status, "ok")
        self.assertEqual(len(CALLS["deleted"]), 3)                                               # temp files removed

    def test_telugu_and_hindi_instructions(self):
        s, _ = catalog.resolve_narration_settings({"language": "te"})
        self.assertIn("Telugu script", gemini.build_prompt(s, self.items()))
        s, _ = catalog.resolve_narration_settings({"language": "hi"})
        self.assertIn("Devanagari", gemini.build_prompt(s, self.items()))


class LengthControl(Base):
    def test_out_of_range_triggers_one_adjust_pass(self):
        CALLS["script"] = [words(LO // 3), words((LO + HI) // 2)]
        res = self.run_gen()
        self.assertEqual(len(CALLS["contents"]), 2)                       # generate + adjust
        self.assertEqual(res.word_count, (LO + HI) // 2)
        self.assertIn("Current narration", CALLS["contents"][1][0])

    def test_in_range_is_left_alone(self):
        self.run_gen()
        self.assertEqual(len(CALLS["contents"]), 1)

    def test_adjust_uses_measured_duration(self):
        CALLS["script"] = words(100)
        out = gemini.adjust_narration("key", "m", words(100), SETTINGS, measured_seconds=20)   # 30s wanted -> ~150 words
        self.assertIn("about 20 seconds", CALLS["contents"][0][0])
        self.assertIn("between 142 and 158", CALLS["contents"][0][0])
        self.assertTrue(out)


class Degradation(Base):
    def test_video_falls_back_to_proxy_then_notes(self):
        first = {"n": 0}

        def fail(path):                              # original clip.mp4 is rejected, proxies/others are fine
            return os.path.basename(path) == "clip.mp4"
        CALLS["fail_upload"] = fail
        res = self.run_gen()
        self.assertEqual(res.analysis_status, "ok")  # proxy keeps picture AND sound -> nothing lost
        self.assertTrue(any("m2_proxy.mp4" in u[0] for u in CALLS["uploads"]))

    def test_audio_only_fallback_is_reported(self):
        CALLS["fail_upload"] = lambda p: p.endswith(".mp4")
        res = self.run_gen()
        self.assertEqual(res.analysis_status, "partial")
        self.assertTrue(any("Only the audio" in n for n in res.notes))
        self.assertIn("Note about the media", CALLS["contents"][0][-1])

    def test_unanalyzable_file_is_skipped_not_fatal(self):
        CALLS["fail_upload"] = lambda p: p.endswith("voice.wav")
        res = self.run_gen()
        self.assertEqual(res.analysis_status, "partial")
        self.assertTrue(any("note.wav" in n and "skipped" in n for n in res.notes))

    def test_nothing_analyzable(self):
        CALLS["fail_upload"] = lambda p: True
        with self.assertRaises(gemini.GeminiError) as cm:
            self.run_gen()
        self.assertEqual(cm.exception.code, "no_media")


class Errors(Base):
    def test_missing_key(self):
        with self.assertRaises(gemini.GeminiError) as cm:
            gemini.generate_narration("", "m", self.items(), SETTINGS, os.path.join(self.tmp, "w"))
        self.assertEqual(cm.exception.code, "not_configured")

    def check(self, exc, code):
        CALLS["errors"] = [exc]
        with self.assertRaises(gemini.GeminiError) as cm:
            self.run_gen()
        self.assertEqual(cm.exception.code, code)
        self.assertNotIn("key", cm.exception.user_message.lower().replace("keys", ""))      # no secrets / internals
        self.assertEqual(len(CALLS["deleted"]), 3)                                           # still cleaned up

    def test_quota(self):
        self.check(FakeAPIError(429, "RESOURCE_EXHAUSTED quota"), "quota")

    def test_invalid_key(self):
        self.check(FakeAPIError(400, "API key not valid. Please pass a valid API key."), "invalid_key")

    def test_unavailable(self):
        self.check(FakeAPIError(503, "overloaded"), "unavailable")

    def test_upload_quota_is_fatal_not_skipped(self):
        CALLS["fail_upload"] = None

        def boom(file, config=None):
            raise FakeAPIError(429, "quota")
        orig = FakeFiles.upload
        FakeFiles.upload = lambda self, file, config=None: boom(file, config)
        try:
            with self.assertRaises(gemini.GeminiError) as cm:
                self.run_gen()
            self.assertEqual(cm.exception.code, "quota")
        finally:
            FakeFiles.upload = orig


class CleanScript(unittest.TestCase):
    def test_strips_markdown_and_labels(self):
        raw = '```\nNarration: **Welcome** to the _show_.\n- First point\n[Music fades in]\n"Thanks."\n```'
        out = gemini.clean_script(raw)
        self.assertNotIn("**", out)
        self.assertNotIn("Music fades", out)
        self.assertNotIn("```", out)
        self.assertTrue(out.startswith("Welcome"))

    def test_unwraps_full_quotes(self):
        self.assertEqual(gemini.clean_script('"Hello there."'), "Hello there.")
        self.assertEqual(gemini.clean_script('He said "hi" and "bye"'), 'He said "hi" and "bye"')


class MediaValidation(Base):
    def test_real_files_pass_and_kinds_are_detected(self):
        for name, kind in (("clip.mp4", "video"), ("pic.png", "image"), ("voice.wav", "audio")):
            info = media.validate_file(os.path.join(self.tmp, name), name)
            self.assertEqual(info.kind, kind)

    def test_content_must_match_extension(self):
        fake = os.path.join(self.tmp, "fake.mp4")
        shutil.copy(os.path.join(self.tmp, "pic.png"), fake)           # a PNG pretending to be an mp4
        with self.assertRaises(media.MediaError):
            media.validate_file(fake, "fake.mp4")
        with self.assertRaises(media.MediaError):
            media.validate_file(os.path.join(self.tmp, "clip.mp4"), "malware.exe")

    def test_size_limits_per_kind(self):
        class Tiny(media.Config):
            MAX_IMAGE_MB = 0
        with self.assertRaises(media.MediaError) as cm:
            media.validate_file(os.path.join(self.tmp, "pic.png"), "pic.png", Tiny)
        self.assertEqual(cm.exception.code, "file_too_large")


if __name__ == "__main__":
    unittest.main()
