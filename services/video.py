"""
Builds a finished reel (mp4) out of:
  - an ordered mix of uploaded VIDEO clips and IMAGES, normalised to one frame size
    (images become still clips of a configurable length; videos are cropped to fill)
  - an AI voiceover track (from services/tts.py) - always the primary audio
  - optional background music (a library song and/or uploaded audio files), kept quiet
  - optionally the clips' own sound at a low volume

Uses moviepy (which itself drives ffmpeg under the hood). We rely on the
`imageio-ffmpeg` package (see requirements.txt) so a working ffmpeg binary
is available automatically - no system-level ffmpeg install required.
"""
import logging
import os

import numpy as np
from moviepy.editor import (
    VideoFileClip,
    ImageClip,
    AudioFileClip,
    CompositeAudioClip,
    concatenate_videoclips,
    concatenate_audioclips,
)
from moviepy.video.fx.all import loop as loop_video
from moviepy.audio.fx.all import audio_loop

log = logging.getLogger(__name__)

# A still image is never held longer than this when we stretch a slideshow to fit the voiceover.
MAX_IMAGE_HOLD_SECONDS = 20.0


class ReelBuildError(Exception):
    pass


# ----------------------------------------------------------------------------
# Frames
# ----------------------------------------------------------------------------

def _fit_to_frame(clip, width, height):
    """Resize+crop a clip so it fully covers a width x height frame
    (like `object-fit: cover` in CSS), keeping the center."""
    clip_ratio = clip.w / clip.h
    target_ratio = width / height

    if clip_ratio > target_ratio:
        clip = clip.resize(height=height)
        clip = clip.crop(x_center=clip.w / 2, width=width)
    else:
        clip = clip.resize(width=width)
        clip = clip.crop(y_center=clip.h / 2, height=height)

    return clip


def image_frame(path, width, height, mode="crop"):
    """
    Render an image into an exact width x height RGB numpy frame, never distorting it.
      crop - scale to cover the frame and trim the overflow (default)
      fit  - whole image visible, black bars
      fill - whole image visible, bars filled with a dimmed, blurred copy of the image
    Honours EXIF rotation and flattens transparency onto black. Done with Pillow so it
    does not depend on moviepy's resizer.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    resample = getattr(Image, "Resampling", Image)
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src)
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (0, 0, 0))
            flat.paste(rgba, mask=rgba.split()[-1])
            im = flat
        else:
            im = im.convert("RGB")
        if max(im.size) > 6000:                       # keep memory sane for huge photos
            im.thumbnail((6000, 6000), resample.LANCZOS)

        if mode == "fit":
            canvas = Image.new("RGB", (width, height), (0, 0, 0))
            fg = ImageOps.contain(im, (width, height), resample.LANCZOS)
            canvas.paste(fg, ((width - fg.width) // 2, (height - fg.height) // 2))
        elif mode == "fill":
            small = ImageOps.fit(im, (max(2, width // 8), max(2, height // 8)), resample.BILINEAR)
            canvas = small.filter(ImageFilter.GaussianBlur(4)).resize((width, height), resample.BILINEAR)
            canvas = ImageEnhance.Brightness(canvas).enhance(0.6)
            fg = ImageOps.contain(im, (width, height), resample.LANCZOS)
            canvas.paste(fg, ((width - fg.width) // 2, (height - fg.height) // 2))
        else:
            canvas = ImageOps.fit(im, (width, height), resample.LANCZOS, centering=(0.5, 0.5))
        return np.array(canvas)


def stretch_image_durations(durations, is_image, target):
    """
    If the visuals are shorter than the voiceover, hold the still images longer (up to
    MAX_IMAGE_HOLD_SECONDS each) instead of looping the whole reel. Videos are never slowed.
    Returns a new list of durations. Pure function, easy to test.
    """
    total = sum(durations)
    n_images = sum(is_image)
    if total >= target or not n_images:
        return list(durations)
    extra = (target - total) / n_images
    out = []
    for d, img in zip(durations, is_image):
        if img:
            d = min(d + extra, max(d, MAX_IMAGE_HOLD_SECONDS))
        out.append(d)
    return out


# ----------------------------------------------------------------------------
# Audio
# ----------------------------------------------------------------------------

def _soft_limit(samples):
    """Smoothly squash peaks above 0.9 so layered audio never hard-clips."""
    a = np.abs(samples)
    over = a > 0.9
    if not over.any():
        return samples
    return np.where(over, np.sign(samples) * (0.9 + 0.1 * np.tanh((a - 0.9) / 0.1)), samples)


def _music_track(paths, duration, volume, opened):
    """Uploaded/library music -> one quiet track exactly `duration` long (looped or trimmed)."""
    clips = []
    for p in paths:
        try:
            c = AudioFileClip(p)
        except Exception:  # noqa: BLE001 - music is a nice-to-have; never fail the reel over it
            log.warning("Skipping unreadable music file %s", os.path.basename(p), exc_info=True)
            continue
        opened.append(c)
        clips.append(c)
    if not clips:
        return None
    track = concatenate_audioclips(clips) if len(clips) > 1 else clips[0]
    track = track.volumex(volume)
    if track.duration < duration:
        return audio_loop(track, duration=duration)
    return track.subclip(0, duration)


def _close_all(items):
    for c in items:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------

def build_reel(video_paths, voiceover_path, output_path, width=1080, height=1920,
               song_path=None, song_volume=0.15, thumbnail_path=None, fps=30,
               on_stage=None, media_items=None, music_paths=None, image_seconds=3.0,
               image_fit="crop", original_volume=0.0, voiceover_volume=1.0):
    """
    video_paths:   legacy argument - paths of video clips (used when media_items is not given)
    media_items:   ordered [{"path": ..., "kind": "video" | "image"}] - videos and images, in reel order
    voiceover_path: path to the mp3 generated by ElevenLabs
    output_path:   where to write the final .mp4
    song_path:     optional library song (background music)
    music_paths:   optional uploaded audio files, played one after another as background music
    image_seconds: how long each still image is shown (before any stretch to match the voiceover)
    image_fit:     "crop" | "fit" | "fill" (see image_frame)
    original_volume / song_volume / voiceover_volume: 0..1 mix levels
    thumbnail_path: optional path to save a jpg thumbnail of the reel
    on_stage:      optional callback(name) with "clips" then "rendering", so the UI can show real progress
    Returns {"path": output_path, "duration": seconds}.
    """
    stage = on_stage or (lambda name: None)
    if media_items is None:
        media_items = [{"path": p, "kind": "video"} for p in (video_paths or [])]
    if not media_items:
        raise ReelBuildError("At least one video or image is required.")

    opened = []              # everything we must close at the end
    narration = None
    final_video = None
    target_duration = None

    try:
        stage("clips")
        try:
            narration = AudioFileClip(voiceover_path)
        except Exception as exc:
            raise ReelBuildError(f"Could not read generated voiceover audio: {exc}") from exc
        opened.append(narration)
        target_duration = narration.duration
        if not target_duration or target_duration <= 0:
            raise ReelBuildError("Generated voiceover audio has zero length.")

        # 1. every item -> a clip of exactly width x height
        clips, is_image = [], []
        for item in media_items:
            path, name = item["path"], os.path.basename(item["path"])
            if item["kind"] == "image":
                try:
                    frame = image_frame(path, width, height, image_fit)
                except Exception as exc:
                    raise ReelBuildError(f"Could not read image file '{name}': {exc}") from exc
                clips.append(ImageClip(frame).set_duration(float(image_seconds)))
                is_image.append(True)
            else:
                try:
                    raw = VideoFileClip(path)
                except Exception as exc:
                    raise ReelBuildError(f"Could not read video file '{name}': {exc}") from exc
                opened.append(raw)
                clips.append(_fit_to_frame(raw, width, height))
                is_image.append(False)

        # 2. slideshow-heavy reels: hold images longer rather than looping the whole reel
        new_durations = stretch_image_durations([c.duration for c in clips], is_image, target_duration)
        clips = [c.set_duration(d) if img and abs(c.duration - d) > 1e-6 else c
                 for c, d, img in zip(clips, new_durations, is_image)]

        stitched = concatenate_videoclips(clips, method="compose")
        if stitched.duration < target_duration:
            final_video = loop_video(stitched, duration=target_duration)
        else:
            final_video = stitched.subclip(0, target_duration)

        # 3. audio: voiceover on top, everything else underneath
        mix = [narration.volumex(voiceover_volume) if voiceover_volume != 1.0 else narration]

        music_files = list(music_paths or [])
        if not music_files and song_path and os.path.exists(song_path):
            music_files = [song_path]
        if music_files:
            music = _music_track(music_files, target_duration, song_volume, opened)
            if music is not None:
                mix.insert(0, music)

        if original_volume > 0 and final_video.audio is not None:
            mix.insert(0, final_video.audio.volumex(original_volume))

        if len(mix) == 1:
            final_audio = mix[0]
        else:
            final_audio = CompositeAudioClip(mix).set_duration(target_duration)
            final_audio = final_audio.fl(lambda gf, t: _soft_limit(gf(t)), keep_duration=True)

        final_video = final_video.set_audio(final_audio).set_duration(target_duration)

        if thumbnail_path:
            try:
                final_video.save_frame(thumbnail_path, t=min(0.5, target_duration / 2))
            except Exception:  # noqa: BLE001
                pass  # thumbnail is a nice-to-have, never fail the whole job over it

        stage("rendering")
        final_video.write_videofile(
            output_path,
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            threads=2,
            preset="medium",
            logger=None,
        )

    finally:
        _close_all(opened)
        if final_video is not None:
            _close_all([final_video])

    return {"path": output_path, "duration": target_duration}
