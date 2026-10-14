"""
End-to-end checks for the upgrade (run with the project's real dependencies installed):

    pip install -r requirements.txt
    python -m unittest tests.test_scenarios -v

ElevenLabs and moviepy are replaced by fakes, so no API key, network or ffmpeg is needed.
The module first builds a LEGACY database (old schema, user_id NOT NULL) so the
startup upgrade is exercised on real old data too.
"""
import itertools
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="reeltest_")
DB = os.path.join(TMP, "test.db")
LEGACY_DIR = os.path.join(TMP, "legacy_reels")
os.makedirs(LEGACY_DIR)

os.environ.update(DATABASE_URL=f"sqlite:///{DB}", REEL_STORAGE_DIR=os.path.join(TMP, "storage"),
                  SECRET_KEY="test-secret", GUEST_FREE_REELS="1", GUEST_TRACK_BY_IP="1",
                  GEMINI_API_KEY="test-gemini-key")

# ---- legacy database as the OLD code created it ---------------------------------
con = sqlite3.connect(DB)
con.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, email VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL, created_at DATETIME);
CREATE TABLE reels (id VARCHAR(36) PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title VARCHAR(200),
                    script_text TEXT, song VARCHAR(120), filename VARCHAR(255), thumbnail VARCHAR(255),
                    status VARCHAR(20), error_message TEXT, created_at DATETIME);
""")
con.execute("INSERT INTO users VALUES (1,'legacy','legacy@x.com',?, '2025-01-01 00:00:00')", (generate_password_hash("secret1"),))
con.execute("INSERT INTO reels VALUES ('legacy-reel-1',1,'Old reel','hi',NULL,'old.mp4',NULL,'done',NULL,'2025-01-02 00:00:00')")
con.commit(); con.close()
with open(os.path.join(LEGACY_DIR, "old.mp4"), "wb") as f:
    f.write(b"legacy-bytes")

import config  # noqa: E402
config.Config.LEGACY_REELS_FOLDER = LEGACY_DIR

import reels as reels_module  # noqa: E402


def fake_voiceover(text, output_path, api_key, voice_id, model_id="x"):
    open(output_path, "wb").write(b"mp3")


LAST_BUILD = {}


def fake_build(video_paths, voiceover_path, output_path, width, height, song_path=None,
               thumbnail_path=None, on_stage=None, **kw):
    LAST_BUILD.clear(); LAST_BUILD.update(kw, video_paths=video_paths, song_path=song_path)
    on_stage and on_stage("clips"); on_stage and on_stage("rendering")
    open(output_path, "wb").write(b"fake-mp4-%dx%d" % (width, height))
    if thumbnail_path:
        open(thumbnail_path, "wb").write(b"jpg")
    return {"path": output_path, "duration": 7.5}


reels_module.generate_voiceover = fake_voiceover
reels_module.build_reel = fake_build

# ffmpeg is not needed for these tests: pretend every audio/video file probes fine (5 s, with sound).
from services import gemini as gemini_svc  # noqa: E402
from services import media as media_svc  # noqa: E402
media_svc.probe_media = lambda path, timeout=30: {"duration": 5.0, "has_video": True, "has_audio": True,
                                                  "width": 640, "height": 480}

GEMINI_CALLS = []


def fake_generate(api_key, model, media, settings, workdir, on_stage=None, timeout=0):
    GEMINI_CALLS.append(dict(media=[m["name"] for m in media], settings=settings))
    for st in ("analyzing", "understanding", "writing"):
        on_stage and on_stage(st)
    return gemini_svc.NarrationResult(" ".join(["hello"] * 40), "ok", [])


gemini_svc.generate_narration = fake_generate

from app import app  # noqa: E402  (runs the schema upgrade on the legacy DB)
from extensions import db  # noqa: E402
from models import Reel, User  # noqa: E402


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _png():
    from io import BytesIO
    from PIL import Image
    buf = BytesIO(); Image.new("RGB", (8, 8), (200, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
FAKE_MP3 = b"ID3" + b"\x00" * 64
PNG = _png()

_ips = itertools.count(1)


class Client:
    def __init__(self):
        self.c = app.test_client()
        self.c.environ_base["REMOTE_ADDR"] = "10.0.%d.%d" % divmod(next(_ips), 250)

    def token(self):
        html = self.c.get("/login").get_data(as_text=True)
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def register(self, name):
        r = self.c.post("/register", data=dict(csrf_token=self.token(), username=name, email=f"{name}@x.com",
                                               password="secret1", confirm_password="secret1"))
        assert r.status_code == 302, r.get_data(as_text=True)[:300]

    def login(self, name, pw="secret1"):
        return self.c.post("/login", data=dict(csrf_token=self.token(), username=name, password=pw))

    def api(self, method, url, **kw):
        h = {"X-CSRFToken": self.token(), "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        return getattr(self.c, method)(url, headers=h, **kw)

    def create(self, platform="instagram_reel", files=None, **extra):
        from io import BytesIO
        data = dict(platform=platform, title=extra.pop("title", "My reel"), text="Hello world", voice="", song="")
        data.update(extra)
        data["files"] = [(BytesIO(c), n) for n, c in (files or [("clip.mp4", FAKE_MP4)])]
        return self.api("post", "/create", data=data, content_type="multipart/form-data")

    def narrate(self, files=None, **settings):
        from io import BytesIO
        data = dict(duration_seconds="30", language="te", tone="energetic", style="storytelling",
                    audience="students", speed="normal", instructions="make it fun")
        data.update(settings)
        data["files"] = [(BytesIO(c), n) for n, c in (files or [("clip.mp4", FAKE_MP4), ("pic.png", PNG)])]
        return self.api("post", "/api/narration/generate", data=data, content_type="multipart/form-data")

    def wait_draft(self, draft_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            d = self.api("get", f"/api/narration/{draft_id}/status").get_json()
            if d["status"] in ("ready", "failed"):
                return d
            time.sleep(0.1)
        raise AssertionError("narration never finished")

    def wait_done(self, reel_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            d = self.api("get", f"/api/reel/{reel_id}/status").get_json()
            if d["status"] in ("done", "failed"):
                return d
            time.sleep(0.1)
        raise AssertionError("reel never finished")

    def make_reel(self, **kw):
        r = self.create(**kw)
        assert r.status_code == 201, r.get_data(as_text=True)
        return self.wait_done(r.get_json()["reel_id"])

    def gallery_ids(self):
        return [i["id"] for i in self.api("get", "/api/reels").get_json()["items"]]


class Scenarios(unittest.TestCase):
    def test_00_legacy_data_survives_upgrade(self):
        with app.app_context():
            r = db.session.get(Reel, "legacy-reel-1")
            self.assertIsNotNone(r)
            self.assertEqual((r.reel_format, r.width, r.height), ("custom", 1080, 1920))
            self.assertEqual(r.video_path, "users/1/reels/old.mp4")   # moved out of public static/
        self.assertFalse(os.path.exists(os.path.join(LEGACY_DIR, "old.mp4")))
        u = Client(); self.assertEqual(u.login("legacy").status_code, 302)
        self.assertIn("legacy-reel-1", u.gallery_ids())
        self.assertEqual(u.c.get("/reel/legacy-reel-1/video").data, b"legacy-bytes")

    def test_01_02_gallery_isolation(self):
        a, b = Client(), Client(); a.register("alice"); b.register("bob")
        ra, rb = a.make_reel(title="A1"), b.make_reel(title="B1")
        self.assertEqual(a.gallery_ids(), [ra["id"]])
        self.assertEqual(b.gallery_ids(), [rb["id"]])
        self.assertNotIn(rb["id"], a.gallery_ids())

    def test_03_cross_user_access_denied(self):
        a, b = Client(), Client(); a.register("carol"); b.register("dave")
        rb = b.make_reel(title="private")["id"]
        for method, url in [("get", f"/reel/{rb}"), ("get", f"/reel/{rb}/video"), ("get", f"/reel/{rb}/download"),
                            ("get", f"/reel/{rb}/thumbnail"), ("get", f"/api/reel/{rb}"),
                            ("get", f"/api/reel/{rb}/status"), ("delete", f"/api/reel/{rb}")]:
            self.assertEqual(getattr(a, "api")(method, url).status_code, 404, url)
        self.assertEqual(a.api("patch", f"/api/reel/{rb}", json={"title": "pwned"}).status_code, 404)
        self.assertEqual(b.gallery_ids(), [rb])                       # untouched
        with app.app_context():
            self.assertEqual(db.session.get(Reel, rb).title, "private")

    def test_04_05_guest_trial(self):
        g = Client()
        first = g.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(g.wait_done(first.get_json()["reel_id"])["status"], "done")
        second = g.create()
        self.assertEqual(second.status_code, 401)
        self.assertEqual(second.get_json()["error"], "free_limit_reached")
        self.assertIn("login_url", second.get_json())
        # refresh / clearing cookies must not reset it (same IP, new cookie jar)
        g2 = Client(); g2.c.environ_base["REMOTE_ADDR"] = g.c.environ_base["REMOTE_ADDR"]
        self.assertEqual(g2.create().status_code, 401)

    def test_05b_guest_reel_private_and_claimed_on_register(self):
        g = Client(); rid = g.make_reel(title="free one")["id"]
        other = Client()
        self.assertEqual(other.api("get", f"/api/reel/{rid}").status_code, 404)
        g.register("erin")
        self.assertEqual(g.gallery_ids(), [rid])
        with app.app_context():
            self.assertTrue(db.session.get(Reel, rid).video_path.startswith(f"users/{User.query.filter_by(username='erin').one().id}/reels/"))
        self.assertEqual(g.c.get(f"/reel/{rid}/video").status_code, 200)

    def test_06_07_08_formats_stored(self):
        u = Client(); u.register("frank")
        cases = [("instagram_reel", {}, 1080, 1920, "9:16"), ("youtube_short", {}, 1080, 1920, "9:16"),
                 ("custom", {"width": "1080", "height": "1080"}, 1080, 1080, "1:1"),
                 ("tiktok", {"width": "100", "height": "100"}, 1080, 1920, "9:16")]   # client dims ignored
        for key, extra, w, h, ratio in cases:
            d = u.make_reel(platform=key, **extra)
            self.assertEqual((d["platform"], d["width"], d["height"], d["aspect_ratio"]), (key, w, h, ratio))
        self.assertEqual(u.create(platform="myspace").status_code, 400)
        self.assertEqual(u.create(platform="custom", width="5", height="5").status_code, 400)
        self.assertEqual(u.api("get", "/api/reels?platform=youtube_short").get_json()["total"], 1)
        self.assertEqual(u.api("get", "/api/reels?q=zzz").get_json()["total"], 0)

    def test_09_delete_only_own(self):
        a, b = Client(), Client(); a.register("gina"); b.register("hank")
        ra, rb = a.make_reel()["id"], b.make_reel()["id"]
        self.assertEqual(a.api("delete", f"/api/reel/{ra}").status_code, 200)
        self.assertEqual(a.gallery_ids(), [])
        self.assertEqual(b.gallery_ids(), [rb])

    def test_10_logout_blocks_private_pages(self):
        u = Client(); u.register("ivy"); rid = u.make_reel()["id"]
        u.c.post("/logout", data={"csrf_token": u.token()})
        self.assertEqual(u.c.get("/gallery").status_code, 302)
        self.assertEqual(u.api("get", "/api/reels").status_code, 401)
        self.assertEqual(u.c.get(f"/reel/{rid}/video").status_code, 404)

    # ---- mixed media -------------------------------------------------------------------
    def test_11_mixed_media_reel_keeps_order_and_kinds(self):
        u = Client(); u.register("kim")
        r = u.create(files=[("a.png", PNG), ("b.mp4", FAKE_MP4), ("song.mp3", FAKE_MP3), ("c.png", PNG)],
                     image_seconds="4", image_fit="fit", music_volume="20", original_volume="5")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        d = u.wait_done(r.get_json()["reel_id"])
        self.assertEqual(d["status"], "done")
        self.assertEqual([os.path.basename(i["path"])[3:].split(".")[-1] for i in LAST_BUILD["media_items"]],
                         ["png", "mp4", "png"])                                  # audio is not a visual
        self.assertEqual([i["kind"] for i in LAST_BUILD["media_items"]], ["image", "video", "image"])
        self.assertEqual(len(LAST_BUILD["music_paths"]), 1)
        self.assertEqual((LAST_BUILD["image_seconds"], LAST_BUILD["image_fit"]), (4.0, "fit"))
        self.assertAlmostEqual(LAST_BUILD["song_volume"], 0.2)
        self.assertAlmostEqual(LAST_BUILD["original_volume"], 0.05)
        self.assertEqual(d["narration"]["media_types"], ["audio", "image", "video"])
        self.assertEqual(d["narration"]["media_count"], 4)
        self.assertEqual(d["narration"]["status"], "manual")

    def test_12_bad_media_is_rejected(self):
        u = Client(); u.register("lee")
        cases = [("virus.exe", FAKE_MP4), ("fake.mp4", PNG), ("fake.png", FAKE_MP4), ("empty.jpg", b"")]
        for name, content in cases:
            r = u.create(files=[(name, content)])
            self.assertEqual(r.status_code, 400, name)
        r = u.create(files=[("only.mp3", FAKE_MP3)])                            # audio alone is not a reel
        self.assertEqual(r.status_code, 400)
        self.assertIn("video or image", r.get_json()["message"])
        self.assertEqual(u.gallery_ids(), [])                                    # nothing was created

    # ---- AI narration ----------------------------------------------------------------------
    def test_13_narration_then_reel_from_draft(self):
        u = Client(); u.register("mia")
        r = u.narrate()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        draft_id = r.get_json()["draft_id"]
        d = u.wait_draft(draft_id)
        self.assertEqual(d["status"], "ready")
        self.assertEqual(d["word_count"], 40)
        self.assertEqual([m["kind"] for m in d["media"]], ["video", "image"])
        self.assertNotIn("path", d["media"][0])                                   # no server paths leak
        self.assertEqual(GEMINI_CALLS[-1]["media"], ["clip.mp4", "pic.png"])
        self.assertEqual(GEMINI_CALLS[-1]["settings"]["language"], "te")
        self.assertEqual(u.c.get(d["media"][1]["preview_url"]).data, PNG)         # private preview works for the owner

        # reel from the saved draft: no files re-uploaded, script untouched -> "ai"
        ok = u.api("post", "/create", data=dict(platform="instagram_reel", title="t", text=d["script"], voice="",
                                                song="", draft_id=draft_id), content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 201, ok.get_data(as_text=True))
        done = u.wait_done(ok.get_json()["reel_id"])
        self.assertEqual((done["narration"]["status"], done["narration"]["language"], done["narration"]["requested_seconds"]),
                         ("ai", "te", 30))
        self.assertEqual([i["kind"] for i in LAST_BUILD["media_items"]], ["video", "image"])
        # edited script -> "ai_edited"
        ok2 = u.api("post", "/create", data=dict(platform="instagram_reel", title="t", text="my own words", voice="",
                                                 song="", draft_id=draft_id), content_type="multipart/form-data")
        self.assertEqual(u.wait_done(ok2.get_json()["reel_id"])["narration"]["status"], "ai_edited")

    def test_14_voiceover_length_is_flagged(self):
        u = Client(); u.register("noa")
        draft_id = u.narrate(duration_seconds="60").get_json()["draft_id"]
        d = u.wait_draft(draft_id)
        r = u.api("post", "/create", data=dict(platform="instagram_reel", title="t", text=d["script"], voice="",
                                               song="", draft_id=draft_id), content_type="multipart/form-data")
        done = u.wait_done(r.get_json()["reel_id"])                               # fake probe says the voice is 5 s
        self.assertEqual(done["duration_warning"]["requested_seconds"], 60)
        self.assertEqual(done["duration_warning"]["actual_seconds"], 5)
        self.assertEqual(done["duration_warning"]["draft_id"], draft_id)

    def test_15_drafts_are_private(self):
        a, b = Client(), Client(); a.register("oli"); b.register("pam")
        draft_id = a.narrate().get_json()["draft_id"]
        d = a.wait_draft(draft_id)
        for method, url in [("get", f"/api/narration/{draft_id}/status"),
                            ("get", d["media"][0]["preview_url"]), ("delete", f"/api/narration/{draft_id}")]:
            self.assertEqual(b.api(method, url).status_code, 404, url)
        r = b.api("post", "/create", data=dict(platform="instagram_reel", title="t", text="x", voice="", song="",
                                               draft_id=draft_id), content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)                                      # cannot build a reel from someone else's media

    def test_16_regenerate_and_validation(self):
        u = Client(); u.register("quinn")
        draft_id = u.narrate().get_json()["draft_id"]; u.wait_draft(draft_id)
        r = u.api("post", f"/api/narration/{draft_id}/regenerate", json=dict(duration_seconds=45, language="hi", tone="friendly"))
        self.assertEqual(r.status_code, 202)
        self.assertEqual(u.wait_draft(draft_id)["settings"]["language"], "hi")
        self.assertEqual(GEMINI_CALLS[-1]["settings"]["duration_seconds"], 45)
        bad = u.api("post", f"/api/narration/{draft_id}/regenerate", json=dict(duration_seconds=2))
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(u.narrate(language="klingon").status_code, 400)
        self.assertEqual(u.narrate(files=[("virus.exe", b"MZ")]).status_code, 400)

    def test_17_ai_unavailable_without_key_but_manual_still_works(self):
        u = Client(); u.register("ray")
        app.config["GEMINI_API_KEY"] = ""
        try:
            r = u.narrate()
            self.assertEqual(r.status_code, 503)
            self.assertNotIn("test-gemini-key", r.get_data(as_text=True))
            self.assertEqual(u.make_reel()["status"], "done")                      # hand-written script path untouched
        finally:
            app.config["GEMINI_API_KEY"] = "test-gemini-key"

    def test_csrf_and_open_redirect(self):
        u = Client()
        self.assertEqual(u.c.post("/login", data={"username": "x", "password": "y"}).status_code, 400)
        u.register("jack"); u.c.post("/logout", data={"csrf_token": u.token()})
        r = u.c.post("/login?next=https://evil.com", data=dict(csrf_token=u.token(), username="jack", password="secret1"))
        self.assertNotIn("evil.com", r.headers["Location"])


if __name__ == "__main__":
    unittest.main()
