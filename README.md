# AI Reel Generator

## NEW: mixed media + Gemini AI narration

Upload **videos, images and audio** (any mix), let **Gemini** watch/listen to them and write the
narration, edit it, then **ElevenLabs** voices it and the existing MoviePy pipeline renders the reel.

```
upload media -> preview/reorder -> narration settings -> Gemini analyses the media (in your order)
  -> editable script (regenerate / edit / approve) -> ElevenLabs voiceover -> MoviePy render -> gallery
```

### Setup
1. `pip install -r requirements.txt` (adds `google-genai`, and pins `Pillow<10` because MoviePy 1.0.3 needs it).
2. Add to `.env` (and to the Render dashboard - `render.yaml` already lists it):
   ```
   GEMINI_API_KEY=your_gemini_api_key
   GEMINI_MODEL=gemini-2.5-flash      # optional; any multimodal Gemini model
   ```
   Without a key the app still works: the *Generate AI Narration* button is disabled and users
   write their script by hand, exactly as before.
3. Start the app. The new `media_drafts` table and the new `reels` columns are added automatically
   at startup by `migrations.py` (idempotent; existing reels are untouched).

### What was added / changed
| Area | Files |
|---|---|
| Media validation (extension + magic bytes + Pillow/ffmpeg probe, per-type size limits) | `services/media.py` |
| Gemini: prompt, multimodal upload, fallbacks, error mapping | `services/gemini.py` |
| Private upload "drafts", ownership, TTL cleanup, rate limits | `services/drafts.py`, `models.py` (`MediaDraft`) |
| Narration API (`/api/narration/...`) | `narration.py` |
| Mixed video + image + audio rendering, audio mix, soft limiter | `services/video.py` |
| Narration options, words-per-minute maths, settings validation | `catalog.py` |
| Create form, previews, drag-reorder, AI panel, script editor | `templates/create.html`, `static/js/create.js`, `static/css/create.css` |
| Quota / reel creation using drafts or direct uploads | `reels.py` |
| ElevenLabs: distinct quota / bad voice / bad key errors | `services/tts.py` |

### How it behaves
- **Image upload fixed**: the picker was restricted to `video/*` and the JS dropped everything else. It now accepts
  `video/*, image/*, audio/*` (+ explicit extensions, since some OSes give `.mkv`/`.avi` no MIME type). The backend
  decides the kind from the extension **and** checks the real file bytes, so a renamed file is rejected. Images are
  never given to `VideoFileClip`: they are rendered with Pillow to an exact frame and become still clips.
- **Image fit** (`crop` default / `fit` black bars / `fill` blurred background) and **seconds per image** (default 3).
  If the narration is longer than the visuals, still images are held longer (max 20 s each) instead of looping the reel.
- **Audio**: voiceover 100 %; background music (library song or *uploaded audio files*, which replace the library song)
  default 15 %; original video sound default 10 %. A soft limiter prevents clipping when layers are combined.
- **Narration length**: target words come from speaking speed (slow 100-120, normal 130-160, fast 160-180 wpm),
  scaled slightly per language (Telugu 0.8x) and tone. If Gemini misses the range, one automatic text-only correction
  pass runs. After ElevenLabs the real voiceover length is measured; if it differs from the request by more than
  5 s (`NARRATION_TOLERANCE_SECONDS`) the result page offers *Adjust narration to ~N seconds* (a text-only rewrite
  using the measured length) - the audio is never stretched.
- **Degrades gracefully**: a video Gemini can't read is retried as a small proxy copy, then without audio, then
  audio-only; anything still unusable is skipped and the user is told ("audio could not be analyzed").
- **Privacy**: drafts live in `storage/drafts/<id>/` (never `static/`), served only through owner-checked routes;
  someone else's draft is a 404. Drafts are deleted after `DRAFT_TTL_HOURS` (24). Guests need their free reel
  available to use AI narration, and narration runs are rate-limited per hour and per draft (Gemini costs money).
- **Errors**: missing/invalid Gemini key, quota, timeout, unsupported/oversized/too many files, ElevenLabs quota or bad
  voice, MoviePy failures all give friendly messages; technical detail and stack traces go to the server log only.

### Adding a language / tone
Add one row to `NARRATION_LANGUAGES` (or `NARRATION_TONES`, `NARRATION_STYLES`, `NARRATION_AUDIENCES`) in `catalog.py`.
The form, validation, prompt and length estimate all read from there.

### Limits (all overridable in `.env`)
Video 150 MB, image 20 MB, audio 50 MB per file; 20 files per reel; `MAX_CONTENT_LENGTH_MB` still caps the whole request.

### Tests
```
python -m unittest tests.test_gemini tests.test_video_helpers -v   # need only Pillow + imageio-ffmpeg
python -m unittest tests.test_scenarios -v                          # full app; needs requirements.txt installed
```

---

## What changed in this upgrade

- **Private per-user galleries.** Every query is scoped by the signed-in user's id in SQL
  (`/api/reels`), and every reel route (view, video, thumbnail, download, rename, delete,
  status) looks the reel up by `id AND owner`. Someone else's reel returns **404**, the same as a
  missing one, so nothing leaks. Identity only ever comes from the session, never from form/URL data.
- **Private storage.** Videos live in `storage/users/<user_id>/reels/` (outside `static/`) and are
  served only through ownership-checked routes (Range requests supported). Set `REEL_STORAGE_DIR`
  to relocate it (on Render, the mounted disk - already set in `render.yaml`).
- **Formats.** `catalog.py` is the single source for platforms (Instagram, YouTube Shorts, TikTok,
  Facebook, Custom), aspect ratios, resolutions and voices. The server decides the render size
  (client-sent dimensions are only honoured for *Custom*, and are range-checked).
- **Free first reel.** Anonymous visitors get `GUEST_FREE_REELS` (default 1). It is enforced on
  the server via a `guest_usage` table keyed by a signed-cookie token **and** a salted IP hash, so
  refreshing, deleting the reel, clearing cookies or using incognito does not reset it. A failed
  generation refunds it. When a guest signs in or registers, their free reel is moved into their
  account. Quota rules live in `services/quota.py` (`PLANS`, `evaluate_quota`) - the hook for
  premium plans, monthly limits or credits later.
- **Also fixed:** CSRF protection on all state-changing requests, open-redirect on `?next=`,
  logout is now POST, raw uploads no longer sit in public `static/`, technical error text is
  logged instead of shown to users.

### Upgrading an existing install
Nothing to run manually: on startup `migrations.upgrade_schema()` (also `python migrations.py`)
adds the new columns, lets `reels.user_id` be NULL, and moves old reels from `static/reels/` into
private storage, labelling them *Custom 1080 x 1920* (what they actually are). It is idempotent.
Back up `app.db` first, and with several gunicorn workers run `python migrations.py` once before
starting them.

### Tests
`python -m unittest tests.test_scenarios -v` runs your 10 scenarios plus the legacy-DB upgrade
with ElevenLabs/moviepy faked (no API key needed).

### Deployment notes
- Set `TRUST_PROXY=1` behind Render/Heroku/nginx so the guest IP check sees real client IPs
  (default on). If many visitors share one public IP (school/office), set `GUEST_TRACK_BY_IP=0`.
- Set `SESSION_COOKIE_SECURE=1` and a real `SECRET_KEY` in production.

---


A Flask web app that turns uploaded video clips + a script into a finished,
vertical (1080x1920) social reel: it generates an AI voiceover of your text
with **ElevenLabs**, stitches your clips together, optionally mixes in
background music, and saves the result to a gallery. Includes user
accounts (sign up / log in) so reel creation is tied to a real account.

---

## 1. What's actually in this build

The original template only had page layouts — the "Create Reel" form
posted to `/create`, but there was no backend code that read the upload,
called any AI voice API, or built a video. This version adds the real,
working pieces:

- **Accounts & login** — `Flask-Login` + hashed passwords (`werkzeug.security`).
  Every reel belongs to one user; galleries are private to their owner.
- **ElevenLabs voiceover** — `services/tts.py` calls the ElevenLabs
  text-to-speech API with your script text.
- **Video stitching** — `services/video.py` uses `moviepy` to crop/resize
  your clips to fill a vertical frame, concatenate them, and lay the
  voiceover (+ optional background song) on top.
- **Background processing** — reel creation runs in a background thread so
  the page doesn't hang or time out; a status page polls `/api/reel/<id>/status`
  until it's ready, then redirects to the gallery.
- **Database** — SQLite by default (zero setup); swap in Postgres via
  `DATABASE_URL` for production if you want.

---

## 2. Project structure

```
.
├── app.py                # Flask app factory, registers blueprints
├── wsgi.py                # entrypoint used by gunicorn in production
├── main.py                # entrypoint for local dev (python main.py)
├── config.py               # all settings, read from environment variables
├── extensions.py            # db / login_manager singletons
├── models.py                # User, Reel database models
├── auth.py                  # /login, /register, /logout
├── reels.py                  # /, /create, /gallery, status API
├── services/
│   ├── tts.py                 # ElevenLabs API call
│   └── video.py                # moviepy video/audio stitching
├── templates/                   # Jinja2 HTML (Bootstrap-based)
├── static/
│   ├── css/
│   ├── songs/                     # background music tracks to choose from
│   ├── reels/                      # generated output videos land here
│   └── uploads/                     # temp storage while a reel is being built
├── requirements.txt
├── Procfile                # for Render/Heroku-style start command
├── render.yaml               # one-click Render Blueprint config
├── runtime.txt                 # pins the Python version
└── .env.example                  # copy to .env for local dev
```

---

## 3. Run it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# now open .env and paste in your real ELEVENLABS_API_KEY (see section 4)

python main.py
```

Open **http://127.0.0.1:5000**, sign up for an account, then go to
**Create Reel**.

> **ffmpeg note:** you do *not* need to install ffmpeg separately —
> the `imageio-ffmpeg` package in `requirements.txt` downloads a working
> ffmpeg binary automatically the first time it's used, on any OS.

---

## 4. Setting / changing your ElevenLabs API key

1. Go to https://elevenlabs.io, sign in (or create a free account).
2. Click your profile icon (bottom-left) → **API Keys** → create/copy a key.
3. Put it in your environment as `ELEVENLABS_API_KEY`:
   - **Locally:** open `.env` and set `ELEVENLABS_API_KEY=sk_...your key...`
     then restart `python main.py`.
   - **On Render:** Dashboard → your service → **Environment** tab →
     edit the `ELEVENLABS_API_KEY` variable → **Save Changes** (this
     redeploys automatically). No code changes needed, ever.
4. To use a different voice, change `ELEVENLABS_VOICE_ID` the same way.
   Browse voices and copy their IDs at https://elevenlabs.io/app/voice-library.

You never need to edit any `.py` file to rotate or change the key — it's
only ever read from the environment (`config.py`).

---

## 5. Deploying so anyone can use it via a link

### Recommended: Render

This app needs a real, persistently-running server (it processes video
in a background thread and writes files to disk) — that's exactly what
Render's **Web Service** gives you, and it's the simplest option.

**Steps:**

1. Push this project to a GitHub repo.
2. Go to https://dashboard.render.com → **New** → **Blueprint**, and
   point it at your repo. Render will read `render.yaml` and configure
   everything automatically (build command, start command, a persistent
   disk for `static/reels`, and the environment variable slots).
   - If you'd rather set it up by hand instead of using the Blueprint:
     **New → Web Service** → connect your repo →
     - Build command: `pip install -r requirements.txt`
     - Start command: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 600`
3. In the service's **Environment** tab, set:
   - `SECRET_KEY` — any long random string
   - `ELEVENLABS_API_KEY` — your key from section 4
   - (optional) `DATABASE_URL` if you want Postgres instead of SQLite
4. Add a **Disk** (Render dashboard → your service → Disks) mounted at
   `static/reels` if you want generated reels to survive restarts —
   `render.yaml` already requests a 1GB disk for this.
5. Deploy. Render gives you a public URL like
   `https://ai-reel-generator.onrender.com` — that's the link you share.

### Why not Vercel

Vercel runs your app as short-lived **serverless functions** with a
strict execution time limit and no persistent disk between requests.
This app calls an external AI API and then encodes video with ffmpeg —
that routinely takes well beyond what a serverless function allows, and
any files saved to disk (uploads, generated reels, the SQLite database)
would vanish between requests. It'll deploy, but reel generation will
fail or time out in production. If you specifically want Vercel, you'd
need to move video processing to an external worker/queue and swap
storage for something like S3 — happy to help you build that version if
you'd like it instead.

**Other good options** if not Render: Railway, Fly.io, or a small
DigitalOcean/AWS EC2 VM — anything that runs a persistent process.

---

## 6. Getting the project as a zip

This whole folder is already structured to be zipped and shared as-is —
if you received it as a `.zip`, you can unzip it and follow section 3
immediately. To regenerate a zip yourself after making changes:

```bash
zip -r ai-reel-generator.zip . -x "venv/*" ".git/*" "static/reels/*.mp4" "*.db"
```

---

## 7. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Reel status stuck on "failed", error mentions API key | `ELEVENLABS_API_KEY` missing/invalid — see section 4 |
| Reel status "failed", error mentions rate limit/quota | You've hit your ElevenLabs character quota for the billing period |
| Video looks stretched/cropped oddly | Very unusual aspect ratio input clip — cropping to 1080x1920 is intentional (fills the frame like Instagram Reels) |
| Upload fails immediately | File too large — raise `MAX_CONTENT_LENGTH_MB` in `.env` |
| Works locally, fails after deploying to Vercel | See section 5 — use Render instead |
| `ModuleNotFoundError` on deploy | Make sure your host is actually running `pip install -r requirements.txt` as the build step |

---

## 8. Security notes

- Passwords are hashed with `werkzeug.security` (never stored in plain text).
- Set a strong, random `SECRET_KEY` in production (never reuse the dev default).
- `.env` is git-ignored — never commit real API keys or secrets.
- Uploaded source clips are deleted automatically after a reel finishes
  building (success or failure) to save disk space.
