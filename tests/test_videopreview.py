"""Silent video previews: frame decoding, and the animation that plays them."""
import gc
import os
import struct
import sys
import tempfile
import threading
import time
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


def header(ppm):
    """The size a PPM frame declares."""
    parts = ppm.split(b"\n", 3)
    width, height = parts[1].split()
    return int(width), int(height)


def body(ppm):
    """The raw RGB bytes of a PPM frame, so pixels can be read without Tk."""
    return ppm.split(b"\n", 3)[3]


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
    """Frames go to Tk as PPM: a tiny header and raw RGB, no compression."""

    def test_the_header_names_the_size(self):
        ppm = videopreview._bgra_to_ppm(bytes(4 * 6), 3, 2, flip=False)
        self.assertEqual(header(ppm), (3, 2))

    def test_bgra_becomes_rgb(self):
        # One blue pixel in BGRA is (255, 0, 0, padding).
        ppm = videopreview._bgra_to_ppm(bytes([255, 0, 0, 0]), 1, 1, flip=False)
        self.assertEqual(list(body(ppm)), [0, 0, 255])

    def test_channels_keep_their_order(self):
        ppm = videopreview._bgra_to_ppm(bytes([10, 20, 30, 0]), 1, 1, flip=False)
        self.assertEqual(list(body(ppm)), [30, 20, 10])

    def test_the_padding_byte_is_dropped(self):
        # RGB32's fourth byte carries nothing; PPM has nowhere to put it.
        ppm = videopreview._bgra_to_ppm(bytes(4 * 4), 2, 2, flip=False)
        self.assertEqual(len(body(ppm)), 2 * 2 * 3)

    def test_flip_reverses_row_order(self):
        black = bytes([0, 0, 0, 0])
        white = bytes([255, 255, 255, 0])
        upright = body(videopreview._bgra_to_ppm(black + white, 1, 2, False))
        flipped = body(videopreview._bgra_to_ppm(black + white, 1, 2, True))
        self.assertEqual(list(upright), [0, 0, 0, 255, 255, 255])
        self.assertEqual(list(flipped), [255, 255, 255, 0, 0, 0])

    def test_short_buffers_do_not_crash(self):
        videopreview._bgra_to_ppm(bytes(4), 4, 4, flip=True)

    @unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
    def test_tk_reads_it(self):
        root = make(tk.Tk)
        root.withdraw()
        try:
            ppm = videopreview._bgra_to_ppm(bytes([10, 20, 30, 0]) * 6, 3, 2,
                                            flip=False)
            image = tk.PhotoImage(data=ppm)
            self.assertEqual((image.width(), image.height()), (3, 2))
            self.assertEqual(image.get(0, 0)[:3], (30, 20, 10))
        finally:
            root.destroy()
            gc.collect()


class TestSamplePositions(unittest.TestCase):
    """Where in a clip the runs come from."""

    def test_one_position_per_run(self):
        self.assertEqual(len(videopreview.positions(100.0, 6)), 6)

    def test_nothing_is_taken_from_the_title_card_or_the_credits(self):
        marks = videopreview.positions(100.0, 6)
        self.assertGreaterEqual(min(marks), 100.0 * videopreview.FIRST_FRACTION)
        self.assertLessEqual(max(marks), 100.0 * videopreview.LAST_FRACTION)

    def test_the_ends_of_the_range_are_used(self):
        marks = videopreview.positions(100.0, 6)
        self.assertAlmostEqual(marks[0], 12.0, places=3)
        self.assertAlmostEqual(marks[-1], 90.0, places=3)

    def test_positions_move_forward(self):
        marks = videopreview.positions(600.0, 8)
        self.assertEqual(marks, sorted(marks))

    def test_samples_bunch_toward_the_middle(self):
        # The point of the bias: more of the preview comes from the middle of
        # the video than from either end.
        marks = videopreview.positions(100.0, 7)
        gaps = [b - a for a, b in zip(marks, marks[1:])]
        self.assertLess(gaps[len(gaps) // 2], gaps[0])
        self.assertLess(gaps[len(gaps) // 2], gaps[-1])

    def test_the_middle_sample_sits_in_the_middle(self):
        marks = videopreview.positions(100.0, 5)
        self.assertAlmostEqual(marks[2], 51.0, places=3)

    def test_a_single_run_starts_early_rather_than_centrally(self):
        self.assertAlmostEqual(videopreview.positions(100.0, 1)[0], 12.0,
                               places=3)

    def test_an_unknown_duration_yields_no_offsets(self):
        self.assertEqual(videopreview.positions(0, 3), [0.0, 0.0, 0.0])

    def test_asking_for_none_gives_none(self):
        self.assertEqual(videopreview.positions(100.0, 0), [])


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


class TestFrameComparison(unittest.TestCase):
    """Spotting a stretch where nothing moves, which plays as a freeze."""

    def frame(self, pixel, count=4000):
        return bytes(pixel) * count

    def test_a_frame_does_not_differ_from_itself(self):
        raw = self.frame([12, 80, 200, 0])
        self.assertEqual(videopreview._difference(
            videopreview._sampled(raw), videopreview._sampled(raw)), 0.0)

    def test_black_against_white_is_the_full_range(self):
        black = videopreview._sampled(self.frame([0, 0, 0, 0]))
        white = videopreview._sampled(self.frame([255, 255, 255, 0]))
        self.assertEqual(videopreview._difference(black, white), 255.0)

    def test_a_small_change_reads_small(self):
        a = videopreview._sampled(self.frame([100, 100, 100, 0]))
        b = videopreview._sampled(self.frame([101, 100, 100, 0]))
        self.assertLess(videopreview._difference(a, b),
                        videopreview.STILL_DIFFERENCE)

    def test_a_real_change_clears_the_threshold(self):
        a = videopreview._sampled(self.frame([100, 100, 100, 0]))
        b = videopreview._sampled(self.frame([140, 100, 100, 0]))
        self.assertGreater(videopreview._difference(a, b),
                           videopreview.STILL_DIFFERENCE)

    def test_empty_input_is_no_difference(self):
        self.assertEqual(videopreview._difference(b"", b"abc"), 0.0)

    def test_sampling_stays_on_one_channel(self):
        # Mixing in the padding byte would make a white frame look like a
        # high-contrast one.
        self.assertEqual(videopreview.SAMPLE_STEP % 4, 0)

    def test_the_hunt_is_bounded(self):
        # Unbounded, a video where nothing ever moves spends the whole time
        # budget looking for motion and never decodes its later runs.
        self.assertGreater(videopreview.STILL_SKIP_BUDGET, 0)
        self.assertLess(videopreview.STILL_SKIP_BUDGET,
                        videopreview.MAX_READS_PER_FRAME
                        * videopreview.DEFAULT_PER_SEGMENT)


class TestCloudOnlyFiles(unittest.TestCase):
    """OneDrive placeholders: the bytes are not here, and fetching them is not
    something hovering a row should start."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "holiday.mp4")
        with open(self.path, "w") as fh:
            fh.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def pretend(self, attributes):
        return mock.patch.object(ui, "file_attributes", return_value=attributes)

    def test_an_ordinary_file_is_not_cloud_only(self):
        self.assertFalse(ui.is_cloud_only(self.path))

    def test_a_placeholder_is_spotted(self):
        for attribute in (ui.FILE_ATTRIBUTE_OFFLINE,
                          ui.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                          ui.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            with self.pretend(attribute | 0x20):
                self.assertTrue(ui.is_cloud_only(self.path))

    def test_a_missing_file_is_not_cloud_only(self):
        self.assertFalse(ui.is_cloud_only(os.path.join(self.tmp.name, "no.mp4")))

    def test_the_pane_says_why_it_is_empty(self):
        meta = ui.describe(self.path, False, "MP4 Video", cloud_only=True)
        self.assertIn("Online only", meta)

    def test_an_ordinary_file_says_nothing_extra(self):
        meta = ui.describe(self.path, False, "MP4 Video")
        self.assertNotIn("Online only", meta)

    def test_an_excerpt_is_not_read_from_one(self):
        path = os.path.join(self.tmp.name, "notes.txt")
        with open(path, "w") as fh:
            fh.write("real content")
        with self.pretend(ui.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS):
            self.assertEqual(ui.read_excerpt(path), "")
        self.assertIn("real content", ui.read_excerpt(path))


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

    def test_every_frame_is_an_image_tk_can_read(self):
        _w, _h, frames = videopreview.frames(self.clips[0], size=(160, 160),
                                             count=4, fps=8)
        for frame in frames:
            self.assertTrue(frame.startswith(b"P6\n"), frame[:8])

    def test_output_keeps_the_source_aspect(self):
        for clip in self.clips:
            width, height, pngs = videopreview.frames(clip, size=(240, 240),
                                                      count=2, fps=8)
            if not pngs:
                continue
            self.assertLessEqual(width, 240)
            self.assertLessEqual(height, 240)

    def test_reported_size_matches_the_frames(self):
        width, height, frames = videopreview.frames(self.clips[0],
                                                    size=(200, 200), count=2)
        if not frames:
            self.skipTest("nothing decoded")
        self.assertEqual(header(frames[0]), (width, height))

    def test_frame_count_is_respected(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(160, 160),
                                           count=3, fps=8)
        self.assertLessEqual(len(pngs), 3)

    def test_budget_stops_early_rather_than_hanging(self):
        _w, _h, pngs = videopreview.frames(self.clips[0], size=(480, 480),
                                           count=200, fps=30,
                                           budget_seconds=0.01)
        self.assertLess(len(pngs), 200)

    def test_several_runs_come_back(self):
        _w, _h, runs = videopreview.segments(self.clips[0], size=(160, 160),
                                             count=4, per_segment=4)
        self.assertGreaterEqual(len(runs), 2)
        self.assertTrue(all(runs), "no run should be empty")

    def test_runs_show_different_parts_of_the_clip(self):
        _w, _h, runs = videopreview.segments(self.clips[0], size=(160, 160),
                                             count=4, per_segment=3)
        openers = [run[0] for run in runs]
        self.assertEqual(len(set(openers)), len(openers),
                         "two runs opened on the same frame")

    def test_each_run_is_offered_as_it_finishes(self):
        handed = []
        _w, _h, runs = videopreview.segments(
            self.clips[0], size=(160, 160), count=3, per_segment=3,
            on_segment=lambda w, h, frames: handed.append(frames))
        self.assertEqual(len(handed), 3)
        # Handed over rather than kept: a frame is 368KB uncompressed, and
        # holding the preview here as well as in the caller cost 40MB.
        self.assertEqual(runs, [])

    def test_saying_no_stops_the_decode(self):
        handed = []

        def once(width, height, frames):
            handed.append(frames)
            return False

        videopreview.segments(self.clips[0], size=(160, 160), count=5,
                              per_segment=3, on_segment=once)
        self.assertEqual(len(handed), 1, "the decode carried on regardless")

    def test_frames_within_a_run_actually_move(self):
        # A run of near-identical frames is what made a preview look like it
        # had paused; the decoder should skip ahead rather than keep copies.
        _w, _h, runs = videopreview.segments(self.clips[0], size=(160, 160),
                                             count=2, per_segment=6)
        for run in runs:
            self.assertEqual(len(set(run)), len(run),
                             "a run repeated a frame")

    def test_a_short_clip_is_played_straight_through(self):
        # Six one second runs would cut holes in a clip only a few seconds
        # long, so it is taken in one piece instead.
        _w, _h, runs = videopreview.segments(self.clips[0], size=(120, 120),
                                             count=6, per_segment=12, fps=1.0)
        self.assertEqual(len(runs), 1)

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

    def test_a_cloud_only_video_is_not_decoded(self):
        # It would download the whole file over the network on hover.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "clip.mp4")
            with open(path, "w") as fh:
                fh.write("x")
            with mock.patch.object(ui.shellicon, "thumbnail_png",
                                   return_value=None), \
                 mock.patch.object(ui, "read_excerpt", return_value=""), \
                 mock.patch.object(ui, "is_cloud_only", return_value=True):
                info = self.app._build_preview_data(path, False)
        self.assertFalse(info["is_video"])
        self.assertIn("Online only", info["meta"])

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

    def test_a_growing_loop_still_advances(self):
        """The frame on screen must move even while frames keep arriving.

        With a counter and `counter % len(frames)`, the counter and the length
        grew in step as each new frame was converted, the remainder stopped
        changing, and the preview held a single frame for as long as the
        decode lasted: 2.75 seconds on one clip.
        """
        self.app.show()
        self.app._start_video(self.frames)
        # Run the loop past the end so the cursor has wrapped, then keep
        # feeding it, which is what happens when a later run lands.
        for _ in range(len(self.frames) + 1):
            self.app._advance_video()
        shown = []
        for _ in range(15):
            self.app._video_pending.append(self.frames[0])
            shown.append(self.app._video_index)
            self.app._advance_video()
        repeats = [a for a, b in zip(shown, shown[1:]) if a == b]
        self.assertEqual(repeats, [], "the preview froze on one frame")

    def test_playback_loops_past_the_last_frame(self):
        self.app.show()
        self.app._start_video(self.frames)
        for _ in range(len(self.frames) + 3):
            self.app._advance_video()
        self.assertTrue(self.app._video_frames, "looping must not empty the list")

    def test_the_frame_clock_asks_for_one_interval(self):
        self.app._video_start = time.monotonic()
        self.app._video_ticks = 1
        delay = self.app._next_delay()
        self.assertAlmostEqual(delay, 1000 / ui.VIDEO_FPS, delta=4)

    def test_a_late_frame_shortens_the_next_wait(self):
        # The whole point: a constant delay after a slow frame loses time
        # every tick, which is what held a nominal 12fps loop to 10.7fps.
        interval = 1.0 / ui.VIDEO_FPS
        self.app._video_start = time.monotonic() - interval * 1.5
        self.app._video_ticks = 1
        self.assertLess(self.app._next_delay(), 1000 / ui.VIDEO_FPS)

    def test_the_wait_is_never_zero(self):
        self.app._video_start = time.monotonic() - 60
        self.app._video_ticks = 1
        self.assertGreaterEqual(self.app._next_delay(), 1)

    def test_playing_asks_for_a_finer_system_timer(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.assertTrue(self.app._fine_timer)

    def test_stopping_gives_the_timer_back(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app._stop_video()
        self.assertFalse(self.app._fine_timer)

    def test_a_longer_clip_does_not_ask_twice(self):
        # timeBeginPeriod and timeEndPeriod are counted, so a second run
        # arriving must not add another outstanding request.
        self.app.show()
        self.app._start_video(self.frames)
        self.app._extend_video(self.frames)
        self.app._stop_video()
        self.assertFalse(self.app._fine_timer)

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
        self.app._done.put(("video", 5, self.frames))
        self.app._pump()
        self.assertTrue(self.app._video_frames)

    def test_the_end_of_the_decode_is_carried_through(self):
        self.app.show()
        self.app._preview_token = 5
        self.app._done.put(("video", 5, self.frames))
        self.app._done.put(("video", 5, None))
        self.app._pump()
        self.assertTrue(self.app._video_final)
        self.assertTrue(self.app._video_frames, "one run should play at the end")

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

    def fake_decoder(self, decoded, runs=1):
        """Stand in for the real decoder, recording what it was asked for."""
        def decode(path, size=None, count=1, per_segment=1, fps=12.0,
                   on_segment=None, **rest):
            decoded.append(path)
            for _ in range(runs):
                if on_segment is not None:
                    if on_segment(2, 2, self.frames) is False:
                        break
            return 2, 2, []
        return decode

    def test_a_stale_video_job_is_never_decoded(self):
        # Sweeping down a list of clips must not queue seconds of decoding
        # for every row it passes.
        decoded = []
        with mock.patch.object(ui.videopreview, "segments",
                               side_effect=self.fake_decoder(decoded)):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 8, r"C:\v\old.mp4", (100, 100)))
            self.run_worker()
        self.assertEqual(decoded, [])

    def test_the_current_video_job_is_decoded(self):
        decoded = []
        with mock.patch.object(ui.videopreview, "segments",
                               side_effect=self.fake_decoder(decoded)):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 9, r"C:\v\clip.mp4", (100, 100)))
            self.run_worker()
        self.assertEqual(decoded, [r"C:\v\clip.mp4"])

    def test_each_run_is_handed_over_as_it_lands(self):
        with mock.patch.object(ui.videopreview, "segments",
                               side_effect=self.fake_decoder([], runs=3)):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 9, r"C:\v\clip.mp4", (100, 100)))
            self.run_worker()
        delivered = [item for item in list(self.app._done.queue)
                     if item[0] == "video" and item[2] is not None]
        self.assertEqual(len(delivered), 3, "runs should arrive one at a time")
        endings = [item for item in list(self.app._done.queue)
                   if item[0] == "video" and item[2] is None]
        self.assertEqual(len(endings), 1, "the end should be announced once")

    def test_a_decode_is_abandoned_when_the_selection_moves(self):
        def decode(path, size=None, count=1, per_segment=1, fps=12.0,
                   on_segment=None, **rest):
            # The first run lands, then the user moves to another row.
            on_segment(2, 2, self.frames)
            self.app._preview_token += 1
            self.kept_going = on_segment(2, 2, self.frames) is not False
            return 2, 2, []

        self.kept_going = None
        with mock.patch.object(ui.videopreview, "segments", side_effect=decode):
            self.app._preview_token = 9
            self.app._jobs.put(("video", 9, r"C:\v\clip.mp4", (100, 100)))
            self.run_worker()
        self.assertFalse(self.kept_going, "the decoder was told to carry on")

    def test_a_later_run_lengthens_the_loop(self):
        self.app.show()
        self.app._start_video(self.frames)
        first = self.app._video_id
        self.app._extend_video(self.frames)
        self.assertEqual(self.app._video_id, first,
                         "playback should not restart when a run is added")
        for _ in range(len(self.frames)):
            self.app._advance_video()
        self.assertEqual(len(self.app._video_frames), len(self.frames) * 2)
        self.assertEqual(self.app._video_pending, [])

    def test_a_later_run_is_converted_over_several_frames(self):
        # Converting a whole run at once cost 180ms on the UI thread, which
        # showed as a hitch in the animation every time a run landed.
        self.app.show()
        self.app._start_video(self.frames)
        before = len(self.app._video_frames)
        self.app._extend_video(self.frames * 4)
        self.assertEqual(len(self.app._video_frames), before,
                         "nothing should be converted on arrival")
        self.app._advance_video()
        self.assertEqual(len(self.app._video_frames),
                         before + ui.VIDEO_CONVERT_PER_TICK)

    def test_playback_starts_on_the_opening_frames_alone(self):
        # Motion should start as soon as a few frames exist, not once the
        # whole run has been converted.
        self.app.show()
        self.app._extend_video(self.frames)
        self.app._extend_video(self.frames * 5)
        self.assertIsNotNone(self.app._video_id, "playback did not start")
        self.assertLessEqual(len(self.app._video_frames),
                             ui.VIDEO_OPENING_FRAMES + ui.VIDEO_CONVERT_PER_TICK)
        self.assertTrue(self.app._video_pending, "the rest should still wait")

    def test_the_first_run_is_enough_to_start_on(self):
        """One run is 0.9s of playback, and decoding now runs ahead of that.

        It did not always: a run used to take about as long to decode as to
        play, so the loop reached its end and showed its opening frame again
        just before the next run landed. Frames now cost 19ms rather than
        77ms, which is about 2.5 seconds of playback per second of work.
        """
        self.app.show()
        self.app._extend_video(self.frames)
        self.assertIsNotNone(self.app._video_id)
        self.assertTrue(self.app._video_frames)

    def test_nothing_plays_before_a_run_arrives(self):
        self.app.show()
        self.assertIsNone(self.app._video_id)
        self.assertEqual(self.app._video_frames, [])

    def test_a_later_run_does_not_restart_playback(self):
        self.app.show()
        self.app._extend_video(self.frames)
        first = self.app._video_id
        self.app._extend_video(self.frames)
        self.assertEqual(self.app._video_id, first)

    def test_a_clip_with_only_one_run_still_plays(self):
        # A short clip yields one run and no second one is ever coming.
        self.app.show()
        self.app._extend_video(self.frames)
        self.app._finish_video()
        self.assertIsNotNone(self.app._video_id)
        self.assertTrue(self.app._video_final)

    def test_the_decoder_finishing_with_nothing_starts_nothing(self):
        self.app.show()
        self.app._finish_video()
        self.assertIsNone(self.app._video_id)
        self.assertEqual(self.app._video_frames, [])

    def test_shutdown_stops_playback(self):
        self.app.show()
        self.app._start_video(self.frames)
        self.app.shutdown()
        self.assertIsNone(self.app._video_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
