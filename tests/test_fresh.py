"""Files created after the index was built: watching for them, and finding them."""
import gc
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

import quickfind
from qf import freshwatch, fsindex, search


def notification(action, name, last=False):
    """One FILE_NOTIFY_INFORMATION record, as the kernel lays it out."""
    encoded = name.encode("utf-16-le")
    size = 12 + len(encoded)
    padding = (-size) % 4
    next_offset = 0 if last else size + padding
    return struct.pack("<III", next_offset, action, len(encoded)) + encoded \
        + b"\x00" * padding


class TestNotificationParsing(unittest.TestCase):
    def parse(self, raw):
        return list(freshwatch.FolderWatcher._parse(raw, len(raw)))

    def test_a_created_file(self):
        raw = notification(freshwatch.FILE_ACTION_ADDED, "report.pdf", last=True)
        self.assertEqual(self.parse(raw), [("report.pdf", False)])

    def test_a_deleted_file(self):
        raw = notification(freshwatch.FILE_ACTION_REMOVED, "gone.txt", last=True)
        self.assertEqual(self.parse(raw), [("gone.txt", True)])

    def test_a_rename_is_an_arrival_and_a_departure(self):
        raw = (notification(freshwatch.FILE_ACTION_RENAMED_OLD_NAME, "a.tmp")
               + notification(freshwatch.FILE_ACTION_RENAMED_NEW_NAME, "a.zip",
                              last=True))
        self.assertEqual(self.parse(raw), [("a.tmp", True), ("a.zip", False)])

    def test_several_records_in_one_buffer(self):
        raw = (notification(freshwatch.FILE_ACTION_ADDED, "one.txt")
               + notification(freshwatch.FILE_ACTION_ADDED, "two.txt")
               + notification(freshwatch.FILE_ACTION_ADDED, "three.txt",
                              last=True))
        self.assertEqual([name for name, _ in self.parse(raw)],
                         ["one.txt", "two.txt", "three.txt"])

    def test_unicode_survives(self):
        raw = notification(freshwatch.FILE_ACTION_ADDED, "resume\u00e9 \u4e2d.pdf",
                           last=True)
        self.assertEqual(self.parse(raw), [("resume\u00e9 \u4e2d.pdf", False)])

    def test_a_write_is_not_an_arrival(self):
        raw = notification(3, "edited.txt", last=True)  # FILE_ACTION_MODIFIED
        self.assertEqual(self.parse(raw), [])

    def test_a_truncated_record_stops_the_walk(self):
        raw = notification(freshwatch.FILE_ACTION_ADDED, "fine.txt")
        raw += struct.pack("<III", 0, freshwatch.FILE_ACTION_ADDED, 400)
        self.assertEqual(self.parse(raw), [("fine.txt", False)])

    def test_an_empty_buffer_yields_nothing(self):
        self.assertEqual(self.parse(b""), [])


class TestFiltering(unittest.TestCase):
    def setUp(self):
        self.watcher = freshwatch.FolderWatcher([], lambda p, r, d, m: None)

    def test_downloads_in_progress_are_ignored(self):
        for name in ("a.crdownload", "b.part", "c.tmp", "D.PARTIAL"):
            self.assertTrue(freshwatch.is_partial(name), name)

    def test_finished_files_are_not(self):
        for name in ("a.zip", "b.pdf", "part.txt", "tmp.docx"):
            self.assertFalse(freshwatch.is_partial(name), name)

    def test_excluded_folders_are_skipped(self):
        self.assertTrue(self.watcher._ignored("AppData\\Local\\thing.txt"))
        self.assertTrue(self.watcher._ignored("project\\node_modules\\x.js"))
        self.assertTrue(self.watcher._ignored(".git\\index"))

    def test_ordinary_paths_are_kept(self):
        self.assertFalse(self.watcher._ignored("report.pdf"))
        self.assertFalse(self.watcher._ignored("work\\2026\\report.pdf"))

    def test_a_partial_download_is_skipped(self):
        self.assertTrue(self.watcher._ignored("work\\big file.zip.crdownload"))


class TestWatching(unittest.TestCase):
    """Against the real API, because that is the part that can be wrong."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []
        self.watcher = freshwatch.FolderWatcher(
            [self.tmp.name],
            lambda path, removed, is_dir, mtime: self.seen.append(
                (os.path.basename(path), removed)))

    def tearDown(self):
        self.watcher.stop()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def wait_for(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.seen) >= count:
                return True
            time.sleep(0.02)
        return False

    def touch(self, *parts):
        path = os.path.join(self.tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x")
        return path

    def test_a_new_file_is_reported(self):
        self.assertTrue(self.watcher.start())
        self.touch("brand new.pdf")
        self.assertTrue(self.wait_for(1), "no event arrived")
        self.assertIn(("brand new.pdf", False), self.seen)

    def test_subfolders_are_watched_too(self):
        self.assertTrue(self.watcher.start())
        self.touch("nested", "deeper", "buried.txt")
        self.assertTrue(self.wait_for(1))
        self.assertIn(("buried.txt", False), [s for s in self.seen])

    def test_a_deletion_is_reported(self):
        path = self.touch("doomed.txt")
        self.assertTrue(self.watcher.start())
        os.remove(path)
        self.assertTrue(self.wait_for(1))
        self.assertIn(("doomed.txt", True), self.seen)

    def test_what_it_found_is_described(self):
        details = []
        watcher = freshwatch.FolderWatcher(
            [self.tmp.name],
            lambda path, removed, is_dir, mtime: details.append(
                (os.path.basename(path), is_dir, mtime)))
        self.addCleanup(watcher.stop)
        self.assertTrue(watcher.start())
        os.makedirs(os.path.join(self.tmp.name, "a folder"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not details:
            time.sleep(0.02)
        self.assertTrue(details, "no event arrived")
        name, is_dir, mtime = details[0]
        self.assertEqual(name, "a folder")
        self.assertTrue(is_dir)
        self.assertGreater(mtime, 0)

    def test_a_broken_callback_does_not_stop_the_watch(self):
        def explode(path, removed, is_dir, mtime):
            raise ValueError("boom")

        watcher = freshwatch.FolderWatcher([self.tmp.name], explode)
        self.addCleanup(watcher.stop)
        self.assertTrue(watcher.start())
        with open(os.path.join(self.tmp.name, "one.txt"), "w") as fh:
            fh.write("x")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not watcher.error:
            time.sleep(0.02)
        self.assertIn("boom", watcher.error or "")
        self.assertTrue(any(t.is_alive() for t in watcher._threads))

    def test_stop_ends_the_threads(self):
        self.assertTrue(self.watcher.start())
        self.watcher.stop()
        for thread in self.watcher._threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_no_folders_is_reported_not_raised(self):
        idle = freshwatch.FolderWatcher([], lambda p, r, d, m: None)
        self.assertFalse(idle.start())
        self.assertIn("no folders", idle.error)

    def test_a_missing_folder_is_survivable(self):
        missing = freshwatch.FolderWatcher(
            [os.path.join(self.tmp.name, "nope")], lambda p, r, d, m: None)
        self.assertFalse(missing.start())
        self.assertIn("cannot watch", missing.error)


class TestIndexFromPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "work", "2026")
        os.makedirs(self.folder)
        self.file = os.path.join(self.folder, "quarterly report.pdf")
        with open(self.file, "w") as fh:
            fh.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_path_rebuilds_exactly(self):
        idx = fsindex.from_paths([self.file])
        paths = [idx.path(i) for i in range(len(idx))]
        self.assertIn(self.file, paths)

    def test_ancestors_are_added_as_folders(self):
        idx = fsindex.from_paths([self.file])
        for i in range(len(idx)):
            if idx.path(i) == self.folder:
                self.assertTrue(idx.isdir[i])
                break
        else:
            self.fail("the parent folder was not indexed")

    def test_a_file_is_not_marked_as_a_folder(self):
        idx = fsindex.from_paths([self.file])
        i = [k for k in range(len(idx)) if idx.path(k) == self.file][0]
        self.assertFalse(idx.isdir[i])

    def test_a_folder_given_twice_is_indexed_once(self):
        idx = fsindex.from_paths([self.file, self.folder, self.folder])
        matches = [k for k in range(len(idx)) if idx.path(k) == self.folder]
        self.assertEqual(len(matches), 1)

    def test_siblings_share_their_ancestors(self):
        second = os.path.join(self.folder, "notes.txt")
        with open(second, "w") as fh:
            fh.write("x")
        one = len(fsindex.from_paths([self.file]))
        two = len(fsindex.from_paths([self.file, second]))
        self.assertEqual(two, one + 1)

    def test_a_missing_path_is_skipped(self):
        idx = fsindex.from_paths([os.path.join(self.folder, "ghost.txt")])
        self.assertNotIn("ghost.txt", list(idx.names))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(len(fsindex.from_paths([])), 0)

    def test_the_result_is_searchable(self):
        searcher = search.Searcher(fsindex.from_paths([self.file]))
        found = searcher.search("quarterly", 5)
        self.assertEqual([r.path for r in found], [self.file])

    def test_mtime_is_carried(self):
        idx = fsindex.from_paths([self.file])
        i = [k for k in range(len(idx)) if idx.path(k) == self.file][0]
        self.assertAlmostEqual(idx.mtimes[i], int(os.path.getmtime(self.file)),
                               delta=2)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestFreshResults(unittest.TestCase):
    """The controller side: a downloaded file is findable before any rebuild."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache.pkl")
        os.makedirs(os.path.join(self.tmp.name, "indexed"))
        with open(os.path.join(self.tmp.name, "indexed", "old.txt"), "w") as fh:
            fh.write("x")
        self.cfg = dict(quickfind.DEFAULT_CONFIG)
        self.cfg.update({"roots": [self.tmp.name], "excludes": [],
                         "max_results": 40})
        self._real_cache_path = quickfind.fsindex.cache_path
        quickfind.fsindex.cache_path = lambda: self.cache
        self.controller = make(lambda: quickfind.Controller(self.cfg))
        self.controller._install(fsindex.build_by_walk([self.tmp.name], []))

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

    def arrive(self, name):
        """A file appears after the index was built, and the watcher says so."""
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as fh:
            fh.write("x")
        self.controller.request_fresh(path, False)
        self.controller._drain()
        return path

    def test_the_index_alone_does_not_have_it(self):
        path = os.path.join(self.tmp.name, "downloaded invoice.pdf")
        with open(path, "w") as fh:
            fh.write("x")
        found = self.controller.searcher.search("invoice", 10)
        self.assertEqual(found, [], "the fixture is wrong, not the feature")

    def test_a_fresh_file_is_found(self):
        path = self.arrive("downloaded invoice.pdf")
        results, _note = self.controller.query("invoice")
        self.assertEqual([r.path for r in results], [path])

    def test_a_fresh_file_is_findable_by_abbreviation(self):
        self.arrive("downloaded invoice.pdf")
        results, _note = self.controller.query("dwnld")
        self.assertTrue(any("invoice" in r.name for r in results))

    def test_indexed_files_still_win_on_merit(self):
        self.arrive("unrelated.bin")
        results, _note = self.controller.query("old")
        self.assertTrue(results)
        self.assertEqual(results[0].name, "old.txt")

    def test_a_deleted_fresh_file_disappears(self):
        path = self.arrive("temporary.pdf")
        os.remove(path)
        self.controller.request_fresh(path, True)
        self.controller._drain()
        results, _note = self.controller.query("temporary")
        self.assertEqual(results, [])

    def test_the_same_file_twice_is_listed_once(self):
        path = self.arrive("twice.pdf")
        self.controller.request_fresh(path, False)
        self.controller._drain()
        results, _note = self.controller.query("twice")
        self.assertEqual(len(results), 1)

    def test_a_rebuild_forgets_what_it_now_holds(self):
        self.arrive("rebuilt.pdf")
        self.assertIsNotNone(self.controller.fresh_searcher)
        self.controller._install(fsindex.build_by_walk([self.tmp.name], []))
        self.assertEqual(self.controller._fresh, {})
        results, _note = self.controller.query("rebuilt")
        self.assertEqual(len(results), 1, "the rebuild should have indexed it")

    def test_a_file_seen_during_a_rebuild_is_kept(self):
        index = fsindex.build_by_walk([self.tmp.name], [])
        index.built_at = time.time() - 60      # as if the build began a minute ago
        self.arrive("during.pdf")
        self.controller._install(index)
        self.assertEqual(len(self.controller._fresh), 1)

    def test_the_list_of_fresh_files_is_capped(self):
        for i in range(quickfind.FRESH_MAX + 5):
            self.controller._note_fresh("C:\\x\\file{}.txt".format(i), False)
        self.assertEqual(len(self.controller._fresh), quickfind.FRESH_MAX)

    def test_the_cap_drops_the_oldest_first(self):
        for i in range(quickfind.FRESH_MAX + 1):
            self.controller._note_fresh("C:\\x\\file{}.txt".format(i), False)
        self.assertNotIn("c:\\x\\file0.txt", self.controller._fresh)
        self.assertIn("c:\\x\\file{}.txt".format(quickfind.FRESH_MAX),
                      self.controller._fresh)

    def test_rebuilding_the_fresh_index_is_rate_limited(self):
        self.arrive("first.pdf")
        built_at = self.controller._fresh_at
        self.controller._note_fresh(os.path.join(self.tmp.name, "second.pdf"),
                                    False)
        self.controller._refresh_fresh()
        self.assertEqual(self.controller._fresh_at, built_at,
                         "a burst of arrivals must not rebuild per file")

    def test_a_file_arriving_during_a_search_does_not_break_it(self):
        """The search runs on a worker; the watcher reports on the UI thread.

        Both reach the list of fresh files, and rebuilding the fresh index
        iterated it directly, so an arrival landing mid-query would raise
        "dictionary changed size during iteration" and lose the search. The
        window is narrow in practice, so it is widened here: the iteration is
        slowed down until the other thread is certain to land inside it.
        """

        class Slow(dict):
            def values(self):
                for value in super().values():
                    time.sleep(0.002)
                    yield value

        self.arrive("first.pdf")
        with self.controller._fresh_lock:
            slow = Slow(self.controller._fresh)
            for i in range(20):
                path = os.path.join(self.tmp.name, "f{}.pdf".format(i))
                slow[path.lower()] = (path, False, 0, time.time())
            self.controller._fresh = slow
            self.controller._fresh_dirty = True
            self.controller._fresh_at = 0.0

        errors = []

        def arriving():
            # Lands somewhere inside the slowed-down iteration.
            time.sleep(0.02)
            try:
                self.controller._note_fresh(
                    os.path.join(self.tmp.name, "late.pdf"), False)
            except Exception as exc:          # pragma: no cover - the bug
                errors.append(exc)

        noting = threading.Thread(target=arriving, daemon=True)
        noting.start()
        try:
            self.controller._refresh_fresh(force=True)
        except RuntimeError as exc:           # pragma: no cover - the bug
            errors.append(exc)
        noting.join(timeout=2)
        self.assertEqual([str(e) for e in errors], [])

    def test_the_status_command_mentions_new_files(self):
        self.arrive("brand new.pdf")
        self.controller.command(":status")
        self.assertIn("1 new", self.controller.app.status.cget("text"))

    def test_the_status_command_is_quiet_when_nothing_is_new(self):
        self.controller.command(":status")
        self.assertNotIn("new", self.controller.app.status.cget("text"))

    def test_the_status_line_counts_fresh_matches(self):
        for name in ("invoice one.pdf", "invoice two.pdf"):
            self.arrive(name)
        _results, note = self.controller.query("invoice")
        self.assertIn("2", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
