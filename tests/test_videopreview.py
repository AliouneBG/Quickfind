"""Silent video previews: frame decoding, and the animation that plays them."""
import gc
import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

from qf import shellicon, ui, videopreview

# Shipped with Windows, so the suite has real footage to decode without
# depending on anything personal being present.
SYSTEM_CLIPS = [
    r"C:\Windows\SystemApps\MicrosoftWindows.Client.CoreAI_cw5n1h2txyewy"
    r"\DiscoveryOverlay\Assets\TutorialMode\TutorialModeFirstLast_Dark.webm",
    r"C:\Windows\SystemResources\Windows.UI.SettingsAppThreshold"
    r"\SystemSettings\Assets\SDRSample.mkv",
]


def available_clips():
    return [p for p in SYSTEM_CLIPS if os.path.exists(p)]


def scanlines(png):
    """Raw filtered rows of a PNG, so pixels can be checked without Tk."""
    data, offset = b"", 8
    while offset < len(png):
        length, kind = struct.unpack(">I4s", png[offset:offset + 8])
        if kind == b"IDAT":
            data += png[offset + 8:offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(data)
    width, height = struct.unpack(">II", png[16:24])
    row = width * 4 + 1
    return [raw[i * row:(i + 1) * row] for i in range(height)]


class TestExtensionCheck(unittest.TestCase):
    def test_recognises_common_containers(self):
        for name in ("a.mp4", "b.MKV", "c.mov", "d.webm", "e.avi"):
            self.assertTrue(videopreview.is_video(name), name)

    def test_rejects_everything_else(self):
        for name in ("a.txt", "b.png", "c.pdf", "d.exe", "e"):
            self.assertFalse(videopreview.is_video(name), name)


class TestAspectFit(unittest.TestCase):
    def test_landscape_fits_by_width(self):
        self.assertEqual(videopreview._fit((1920, 1080), (480, 480)), (480, 270))

    def test_portrait_fits_by_height(self):
        self.assertEqual(videopreview._fit((1080, 1920), (480, 480)), (270, 480))

    def test_sides_are_even(self):
        # Odd dimensions upset several colour converters.
        width, height = videopreview._fit((1001, 999), (333, 333))
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)

    def test_unknown_native_size_falls_back_to_the_box(self):
        self.assertEqual(videopreview._fit(None, (320, 240)), (320, 240))

    def test_never_returns_zero(self):
        width, height = videopreview._fit((1920, 1080), (1, 1))
        self.assertGreaterEqual(width, 2)
        self.assertGreaterEqual(height, 2)


class TestPixelConversion(unittest.TestCase):
    def test_bgra_becomes_rgba(self):
        # One blue pixel in BGRA is (255, 0, 0, x).
        png = videopreview._bgra_to_png(bytes([255, 0, 0, 0]), 1, 1, flip=False)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, colour = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((width, height, depth, colour), (1, 1, 8, 6))

    def test_padding_channel_becomes_opaque(self):
        # RGB32's fourth byte is padding and arrives as zero; left alone it
        # would make every frame fully transparent.
        png = videopreview._bgra_to_png(bytes([10, 20, 30, 0]), 1, 1, flip=False)
        # Filter byte, then R, G, B, A.
        self.assertEqual(list(scanlines(png)[0]), [0, 30, 20, 10, 255])

    def test_flip_reverses_row_order(self):
        black = bytes([0, 0, 0, 0])
        white = bytes([255, 255, 255, 0])
        upright = scanlines(videopreview._bgra_to_png(black + white, 1, 2,
                                                      flip=False))
        flipped = scanlines(videopreview._bgra_to_png(black + white, 1, 2,
                                                      flip=True))
        self.assertEqual(upright, flipped[::-1])
        self.assertEqual(list(upright[0]), [0, 0, 0, 0, 255])

    def test_short_buffers_do_not_crash(self):
        videopreview._bgra_to_png(bytes(4), 4, 4, flip=True)


class TestBlankFrames(unittest.TestCase):
    """Opening on a black card would defeat the point of the preview."""

    def frame(self, pixel, count=4000):
        return bytes(pixel) * count

    def test_black_is_blank(self):
        self.assertTrue(videopreview._is_blank(self.frame([0, 0, 0, 0])))

    def test_white_is_blank_despite_the_padding_channel(self):
        self.assertTrue(videopreview._is_blank(self.frame([255, 255, 255, 0])))

    def test_a_flat_colour_is_blank(self):
        self.assertTrue(videopreview._is_blank(self.frame([30, 90, 140, 0])))

    def test_a_picture_is_not_blank(self):
        pixels = bytearray()
        for i in range(4000):
            pixels += bytes([(i * 7) % 256, 40, 200, 0])
        self.assertFalse(videopreview._is_blank(bytes(pixels)))

    def test_an_empty_buffer_is_blank(self):
        self.assertTrue(videopreview._is_blank(b""))


class TestDecoding(unittest.TestCase):
    """Against real files, because the COM plumbing is the risky part."""

    def setUp(self):
        self.clips = available_clips()
        if not self.clips:
            self.skipTest("no system sample videos on this machine")

    def test_decodes_several_distinct_frames(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(240, 240),
                                           count=8, fps=8)
        self.assertGreaterEqual(len(pngs), 2)
        self.assertGreater(len(set(pngs)), 1, "frames should show motion")

    def test_every_frame_is_a_valid_png(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(160, 160),
                                           count=4, fps=8)
        for png in pngs:
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_output_keeps_the_source_aspect(self):
        for clip in self.clips:
            width, height, pngs = videopreview.frames(clip, size=(240, 240),
                                                      count=2, fps=8)
            if not pngs:
                continue
            self.assertLessEqual(width, 240)
            self.assertLessEqual(height, 240)

    def test_reported_size_matches_the_pngs(self):
        width, height, pngs = videopreview.frames(self.clips[0],
                                                  size=(200, 200), count=2)
        if not pngs:
            self.skipTest("nothing decoded")
        png_w, png_h = struct.unpack(">II", pngs[0][16:24])
        self.assertEqual((png_w, png_h), (width, height))

    def test_frame_count_is_respected(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(160, 160),
                                           count=3, fps=8)
        self.assertLessEqual(len(pngs), 3)

    def test_budget_stops_early_rather_than_hanging(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(480, 480),
                                           count=200, fps=30,
                                           budget_seconds=0.01)
        self.assertLess(len(pngs), 200)

    def test_missing_file_returns_nothing(self):
        self.assertEqual(videopreview.frames(r"C:\nope\missing.mp4")[2], [])

    def test_non_video_returns_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notavideo.mp4")
            with open(path, "wb") as fh:
                fh.write(b"this is not a video")
            self.assertEqual(videopreview.frames(path)[2], [])

    def test_repeated_calls_do_not_leak_into_failure(self):
        # MFStartup/MFShutdown are refcounted; unbalanced calls break later use.
        for _ in range(3):
            _w, _h, pngs = videopreview.frames(self.clips[0], size=(120, 120),
                                               count=2, fps=8)
        self.assertTrue(pngs)


class Row:
    def __init__(self, name, path, is_dir=False):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.score = 1.0
        self.index = 0
        self.fuzzy = False


class Stub:
    def __init__(self, rows):
        self.rows = rows

    def query(self, text):
        return (self.rows, "ok") if text.strip() else ([], "")

    def command(self, text):
        pass


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestPlayback(unittest.TestCase):
    def setUp(self):
        self.app = make(lambda: ui.Launcher(Stub([Row("clip.mp4", r"C:\v\clip.mp4")])))
        self.app._ensure_worker = lambda: None
        self.frames = [shellicon.encode_png(2, 2, bytes([n, n, n, 255] * 4))
                       for n in (10, 90, 170)]

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def test_video_rows_are_flagged(self):
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None), \
             mock.patch.object(ui, "read_excerpt", return_value=""):
            info = self.app._build_preview_data(r"C:\v\clip.mp4", False)
        self.assertTrue(info["is_video"])

    def test_documents_are_not_flagged(self):
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None), \
             mock.patch.object(ui, "read_excerpt", return_value=""):
            info = self.app._build_preview_data(r"C:\v\notes.txt", False)
        self.assertFalse(info["is_video"])

    def test_a_video_preview_requests_frames(self):
        self.app.show()
        self.app._preview_token = 7
        self.app._show_preview({"path": r"C:\v\clip.mp4", "image": self.frames[0],
                                "is_thumbnail": True, "is_video": True,
                                "meta": "Video", "excerpt": ""})
        jobs = [j for j in list(self.app._jobs.queue) if j[0] == "video"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0][1], 7)
        self.assertEqual(jobs[0][2], r"C:\v\clip.mp4")

    def test_a_document_preview_requests_nothing(self):
        self.app.show()
        self.app._show_preview({"path": r"C:\v\a.txt", "image": self.frames[0],
                                "is_thumbnail": True, "is_video": False,
                                "meta": "Text", "excerpt": ""})
        self.assertFalse([j for j in list(self.app._jobs.queue) if j[0] == "video"])

    def test_disabled_setting_requests_nothing(self):
        self.app.video_enabled = False
        self.app.show()
        self.app._show_preview({"path": r"C:\v\clip.mp4", "image": self.frames[0],
                                "is_thumbnail": True, "is_video": True,
                                "meta": "Video", "excerpt": ""})
        self.assertFalse([j for j in list(self.app._jobs.queue) if j[0] == "video"])

    def test_playback_starts_and_cycles(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.assertEqual(len(self.app._video_frames), 3)
        first = self.app._video_index
        self.app._advance_video()
        self.assertNotEqual(self.app._video_index, first)

    def test_playback_loops_past_the_last_frame(self):
        self.app.show()
        self.app._start_video(self.frames)
        for _ in range(len(self.frames) + 3):
            self.app._advance_video()
        self.assertTrue(self.app._video_frames, "looping must not empty the list")

    def test_stopping_releases_the_frames(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app._stop_video()
        self.assertEqual(self.app._video_frames, [])
        self.assertIsNone(self.app._video_id)

    def test_a_new_preview_stops_the_previous_clip(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app._show_preview({"path": r"C:\v\other.txt", "image": None,
                                "is_thumbnail": False, "is_video": False,
                                "meta": "Text", "excerpt": "hello"})
        self.assertEqual(self.app._video_frames, [])

    def test_hiding_stops_playback(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app.hide()
        self.assertEqual(self.app._video_frames, [])

    def test_stale_frames_are_ignored(self):
        self.app.show()
        self.app._preview_token = 5
        self.app._done.put(("video", 4, self.frames))
        self.app._pump()
        self.assertEqual(self.app._video_frames, [])

    def test_current_frames_start_playing(self):
        self.app.show()
        self.app._preview_token = 5
        self.app._done.put(("video", 5, self.frames))
        self.app._pump()
        self.assertEqual(len(self.app._video_frames), 3)

    def test_undecodable_frames_do_not_crash(self):
        self.app.show()
        self.app._start_video([b"not a png"])
        self.assertEqual(self.app._video_frames, [])

    def run_worker(self):
        """Drain the queued jobs on a real worker thread, then stop it."""
        self.app._jobs.put(None)
        worker = threading.Thread(target=self.app._work, daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "worker did not finish")

    def test_a_stale_video_job_is_never_decoded(self):
        # Sweeping down a list of clips must not queue a second of decoding
        # for every row it passes.
        decoded = []
        with mock.patch.object(ui.videopreview, "frames",
                               side_effect=lambda *a, **k: decoded.append(a[0])
                               or (0, 0, [])):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 8, r"C:\v\old.mp4", (100, 100)))
            self.run_worker()
        self.assertEqual(decoded, [])

    def test_the_current_video_job_is_decoded(self):
        decoded = []
        with mock.patch.object(ui.videopreview, "frames",
                               side_effect=lambda *a, **k: decoded.append(a[0])
                               or (0, 0, [])):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 9, r"C:\v\clip.mp4", (100, 100)))
            self.run_worker()
        self.assertEqual(decoded, [r"C:\v\clip.mp4"])

    def test_shutdown_stops_playback(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app.shutdown()
        self.assertIsNone(self.app._video_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
