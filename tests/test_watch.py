import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.destroy()
    del _probe
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

import quickfind
from qf import fsindex, watcher


class TestUsnWatcher(unittest.TestCase):
    def test_request_struct_matches_the_windows_layout(self):
        packed = struct.pack("<qIIQQQ", 0, watcher.STRUCTURAL_CHANGES, 0, 3, 1, 0)
        self.assertEqual(len(packed), 40, "READ_USN_JOURNAL_DATA_V0 is 40 bytes")

    def test_reason_mask_covers_structural_changes_only(self):
        mask = watcher.STRUCTURAL_CHANGES
        for reason in (watcher.USN_REASON_FILE_CREATE,
                       watcher.USN_REASON_FILE_DELETE,
                       watcher.USN_REASON_RENAME_NEW_NAME,
                       watcher.USN_REASON_RENAME_OLD_NAME):
            self.assertTrue(mask & reason)
        USN_REASON_DATA_OVERWRITE = 0x00000001
        self.assertFalse(mask & USN_REASON_DATA_OVERWRITE,
                         "plain writes must not wake the indexer")

    def test_drive_letters_are_normalised(self):
        w = watcher.UsnWatcher(["C:\\", "d:", "E:/"], lambda n: None)
        self.assertEqual(w.drives, ["C", "D", "E"])

    def test_unavailable_without_admin(self):
        w = watcher.UsnWatcher(["C:\\"], lambda n: None)
        if fsindex.is_admin():
            self.skipTest("running elevated; the negative path cannot be tested")
        self.assertFalse(w.available())
        self.assertFalse(w.start())
        self.assertIn("administrator", w.error)

    def test_stop_without_start_is_safe(self):
        watcher.UsnWatcher(["C:\\"], lambda n: None).stop()


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestRefreshDebounce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dict(quickfind.DEFAULT_CONFIG)
        self.cfg.update({"roots": [self.tmp.name], "excludes": [],
                         "watch_quiet_seconds": 5, "min_rebuild_seconds": 60})
        self.controller = quickfind.Controller(self.cfg)
        self.rebuilds = []
        self.controller._rebuild = lambda: self.rebuilds.append(time.time())

    def tearDown(self):
        self.controller.stop()
        try:
            self.controller.app.root.destroy()
        except Exception:
            pass
        self.tmp.cleanup()

    def arm(self, age_seconds, last_rebuild_age=999):
        self.controller._change_at = time.time() - age_seconds
        self.controller._last_rebuild = time.time() - last_rebuild_age

    def test_no_refresh_without_changes(self):
        self.controller._maybe_refresh()
        self.assertEqual(self.rebuilds, [])

    def test_waits_for_the_quiet_period(self):
        self.arm(age_seconds=1)
        self.controller._maybe_refresh()
        self.assertEqual(self.rebuilds, [], "should still be within quiet period")

    def test_refreshes_once_quiet(self):
        self.arm(age_seconds=10)
        self.controller._maybe_refresh()
        self.assertEqual(len(self.rebuilds), 1)

    def test_change_is_cleared_after_refresh(self):
        self.arm(age_seconds=10)
        self.controller._maybe_refresh()
        self.controller._maybe_refresh()
        self.assertEqual(len(self.rebuilds), 1, "one burst must rebuild once")
        self.assertIsNone(self.controller._change_at)

    def test_respects_the_floor_between_rebuilds(self):
        self.arm(age_seconds=10, last_rebuild_age=5)
        self.controller._maybe_refresh()
        self.assertEqual(self.rebuilds, [], "too soon after the last rebuild")

    def test_never_refreshes_while_indexing(self):
        self.arm(age_seconds=10)
        self.controller._indexing = True
        self.controller._maybe_refresh()
        self.assertEqual(self.rebuilds, [])

    def test_never_refreshes_while_the_window_is_open(self):
        self.arm(age_seconds=10)
        self.controller.app.show()
        try:
            self.controller._maybe_refresh()
            self.assertEqual(self.rebuilds, [],
                             "must not swap the index during a live search")
        finally:
            self.controller.app.hide()

    def test_refreshes_once_the_window_closes(self):
        self.arm(age_seconds=10)
        self.controller.app.show()
        self.controller._maybe_refresh()
        self.controller.app.hide()
        self.controller._maybe_refresh()
        self.assertEqual(len(self.rebuilds), 1)

    def test_burst_of_changes_coalesces(self):
        for _ in range(500):
            self.controller.request_refresh(1)
        self.controller._drain()
        self.assertEqual(self.controller._pending_changes, 500)
        self.arm(age_seconds=10)
        self.controller._maybe_refresh()
        self.assertEqual(len(self.rebuilds), 1, "500 changes, one rebuild")


if __name__ == "__main__":
    unittest.main(verbosity=2)
