"""
Tests for the pure helpers in services/video.py (image framing, slideshow timing, limiter).
MoviePy is only needed for the full render, so if it is not installed it is stubbed here.

    python -m unittest tests.test_video_helpers -v
"""
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import moviepy.editor  # noqa: F401
except ImportError:
    for name in ("moviepy", "moviepy.editor", "moviepy.video", "moviepy.video.fx", "moviepy.video.fx.all",
                 "moviepy.audio", "moviepy.audio.fx", "moviepy.audio.fx.all"):
        sys.modules[name] = types.ModuleType(name)
    for attr in ("VideoFileClip", "ImageClip", "AudioFileClip", "CompositeAudioClip",
                 "concatenate_videoclips", "concatenate_audioclips"):
        setattr(sys.modules["moviepy.editor"], attr, object)
    sys.modules["moviepy.video.fx.all"].loop = lambda *a, **k: None
    sys.modules["moviepy.audio.fx.all"].audio_loop = lambda *a, **k: None

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from services import video  # noqa: E402


class ImageFrames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="imgtest_")
        # 400x200 landscape: left half red, right half blue
        im = Image.new("RGB", (400, 200), (255, 0, 0))
        im.paste((0, 0, 255), (200, 0, 400, 200))
        cls.land = os.path.join(cls.dir, "land.png")
        im.save(cls.land)
        rgba = Image.new("RGBA", (100, 100), (0, 255, 0, 0))     # fully transparent
        cls.alpha = os.path.join(cls.dir, "alpha.png")
        rgba.save(cls.alpha)

    def test_all_modes_return_exact_frame_size(self):
        for mode in ("crop", "fit", "fill"):
            for w, h in ((1080, 1920), (1080, 1080), (1920, 1080)):
                frame = video.image_frame(self.land, w, h, mode)
                self.assertEqual(frame.shape, (h, w, 3), (mode, w, h))
                self.assertEqual(frame.dtype, np.uint8)

    def test_crop_has_no_bars_fit_has_black_bars(self):
        crop = video.image_frame(self.land, 360, 640, "crop")
        fit = video.image_frame(self.land, 360, 640, "fit")
        self.assertGreater(crop[5, 180].sum(), 0)           # top row is picture
        self.assertEqual(int(fit[5, 180].sum()), 0)          # top row is a black bar
        self.assertGreater(fit[320, 180].sum(), 0)           # middle is the picture

    def test_fill_bars_are_not_black_and_picture_not_distorted(self):
        fill = video.image_frame(self.land, 360, 640, "fill")
        self.assertGreater(fill[5, 90].sum(), 0)             # blurred backdrop, not black
        # contained picture is 360x180 centred: its left half is red-ish, right half blue-ish
        self.assertGreater(int(fill[320, 90][0]), int(fill[320, 90][2]))
        self.assertGreater(int(fill[320, 270][2]), int(fill[320, 270][0]))

    def test_crop_keeps_centre_of_image(self):
        sq = video.image_frame(self.land, 200, 200, "crop")  # centre crop of 400x200 -> the red|blue seam
        self.assertGreater(int(sq[100, 20][0]), 200)
        self.assertGreater(int(sq[100, 180][2]), 200)

    def test_transparency_flattened_to_black(self):
        frame = video.image_frame(self.alpha, 100, 200, "crop")
        self.assertEqual(int(frame.sum()), 0)


class Timing(unittest.TestCase):
    def test_no_change_when_long_enough_or_no_images(self):
        self.assertEqual(video.stretch_image_durations([5, 5], [False, True], 8), [5, 5])
        self.assertEqual(video.stretch_image_durations([5, 5], [False, False], 60), [5, 5])

    def test_images_absorb_the_gap_videos_do_not(self):
        out = video.stretch_image_durations([10, 3, 3], [False, True, True], 30)
        self.assertEqual(out[0], 10)
        self.assertAlmostEqual(sum(out), 30)
        self.assertEqual(out[1], out[2])

    def test_hold_is_capped(self):
        out = video.stretch_image_durations([3, 3, 3], [True, True, True], 300)
        self.assertTrue(all(d <= video.MAX_IMAGE_HOLD_SECONDS for d in out))


class Limiter(unittest.TestCase):
    def test_quiet_audio_untouched_loud_audio_bounded(self):
        quiet = np.array([[0.1, -0.2], [0.5, 0.3]])
        self.assertTrue(np.array_equal(video._soft_limit(quiet), quiet))
        loud = np.array([[1.8, -2.5], [0.95, 0.2]])
        out = video._soft_limit(loud)
        self.assertLessEqual(float(np.abs(out).max()), 1.0)
        self.assertGreater(out[0, 0], 0.9)
        self.assertLess(out[0, 1], -0.9)


if __name__ == "__main__":
    unittest.main()
