import gc
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

import quickfind


def make_tree(root):
    os.makedirs(os.path.join(root, "projects", "alpha"))
    os.makedirs(os.path.join(root, "notes"))
    for path in [
        ("projects", "alpha", "widget.py"),
        ("projects", "alpha", "README.md"),
        ("notes", "meeting-widget.txt"),
        ("toplevel.log",),
    ]:
        open(os.path.join(root, *path), "w").close()


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        make_tree(self.tmp.name)
        self.cache = os.path.join(self.tmp.name, "cache.pkl")

        self.cfg = {
            "hotkey": "alt+space",
            "roots": [self.tmp.name],
            "excludes": [],
            "max_results": 40,
            "refresh_after_hours": 12,
        }
        # Keep the test off the user's real cache file.
        self._real_cache_path = quickfind.fsindex.cache_path
        quickfind.fsindex.cache_path = lambda: self.cache

        self.controller = make(lambda: quickfind.Controller(self.cfg))

    def tearDown(self):
        quickfind.fsindex.cache_path = self._real_cache_path
        self.controller.stop()
        try:
            self.controller.app.root.destroy()
        except Exception:
            pass
        self.controller = None
        gc.collect()
        self.tmp.cleanup()

    def wait_for_index(self, timeout_ms=20000):
        root = self.controller.app.root
        elapsed = [0]

        def poll():
            if self.controller.searcher is not None or elapsed[0] >= timeout_ms:
                root.quit()
                return
            elapsed[0] += 50
            root.after(50, poll)

        root.after(50, poll)
        root.mainloop()
        self.assertIsNotNone(self.controller.searcher, "indexing never finished")

    def test_indexes_then_serves_queries(self):
        self.controller.start(force_rebuild=True)
        self.wait_for_index()

        results, note = self.controller.query("widget")
        names = [r.name for r in results]
        self.assertIn("widget.py", names)
        self.assertIn("meeting-widget.txt", names)
        self.assertIn("ms)", note)

    def test_query_before_index_is_graceful(self):
        results, note = self.controller.query("widget")
        self.assertEqual(results, [])
        self.assertIn("Indexing", note)

    def test_empty_query_returns_nothing(self):
        self.controller.start(force_rebuild=True)
        self.wait_for_index()
        self.assertEqual(self.controller.query("   "), ([], ""))

    def test_no_match_reports_timing(self):
        self.controller.start(force_rebuild=True)
        self.wait_for_index()
        results, note = self.controller.query("zzz-not-here")
        self.assertEqual(results, [])
        self.assertIn("No matches", note)

    def test_cache_is_written_and_reused(self):
        self.controller.start(force_rebuild=True)
        self.wait_for_index()
        self.assertTrue(os.path.exists(self.cache))

        fresh = make(lambda: quickfind.Controller(self.cfg))
        try:
            fresh.start(force_rebuild=False)
            self.assertIsNotNone(fresh.searcher, "cached index not loaded synchronously")
            names = [r.name for r in fresh.query("widget")[0]]
            self.assertIn("widget.py", names)
        finally:
            fresh.stop()
            fresh.app.root.destroy()
            fresh = None
            gc.collect()

    def test_colon_query_shows_command_help(self):
        results, note = self.controller.query(":")
        self.assertEqual(results, [])
        self.assertIn(":reindex", note)

    def test_status_command(self):
        self.controller.start(force_rebuild=True)
        self.wait_for_index()
        self.controller.command(":status")
        self.assertIn("items", self.controller.app.status.cget("text"))

    def test_unknown_command(self):
        self.controller.command(":bogus")
        self.assertIn("Unknown", self.controller.app.status.cget("text"))

    def test_results_are_capped(self):
        # The list is a viewport, so the cap is on how many matches it holds
        # rather than on how many rows are drawn, but it is still a cap.
        self.cfg["max_results"] = 2
        self.controller.start(force_rebuild=True)
        self.wait_for_index()
        results, _ = self.controller.query("e")
        self.assertLessEqual(len(results), 2)


    def test_drain_stops_cleanly_after_destroy(self):
        self.controller.stop()
        self.controller.app.root.destroy()
        self.assertFalse(self.controller.app.alive())
        self.controller._drain()  # must not raise or reschedule
        self.controller.app.set_status("ignored")


if __name__ == "__main__":
    unittest.main(verbosity=2)
