"""MFT misses hardlink names; the supplement walk restores them.

FSCTL_ENUM_USN_DATA returns one record per MFT entry, so a file reachable under
several hardlink names is enumerated under only one. On a real machine this hid
89% of System32 -- cmd.exe, calc.exe, control.exe -- behind their WinSxS paths.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qf import fsindex, search
from test_search import make_index


class TestMerge(unittest.TestCase):
    def test_merge_keeps_both_trees_intact(self):
        a = make_index([r"C:\one\a.txt"])
        b = make_index([r"D:\two\b.txt"])
        fsindex._merge(a, b)
        paths = {a.path(i) for i in range(len(a)) if not a.isdir[i]}
        self.assertEqual(paths, {r"C:\one\a.txt", r"D:\two\b.txt"})

    def test_merge_reparents_without_collision(self):
        a = make_index([r"C:\one\a.txt"])
        before = len(a)
        b = make_index([r"C:\Windows\System32\cmd.exe"])
        fsindex._merge(a, b)
        self.assertEqual(len(a), before + len(b))
        self.assertIn(r"C:\Windows\System32\cmd.exe",
                      {a.path(i) for i in range(len(a))})

    def test_merge_preserves_directory_flags(self):
        a = make_index([r"C:\one\a.txt"])
        b = make_index([r"C:\Windows\System32\cmd.exe"])
        fsindex._merge(a, b)
        by_path = {a.path(i): i for i in range(len(a))}
        self.assertTrue(a.isdir[by_path[r"C:\Windows\System32"]])
        self.assertFalse(a.isdir[by_path[r"C:\Windows\System32\cmd.exe"]])


class TestSupplementedSearch(unittest.TestCase):
    """The supplement overlaps MFT, so the same path can be indexed twice."""

    def setUp(self):
        mft_like = make_index([
            r"C:\Windows\WinSxS\amd64_cmd\cmd.exe",     # what MFT reports
            r"C:\Users\me\notes.txt",
        ])
        walked = make_index([
            r"C:\Windows\System32\cmd.exe",             # the hardlink name
            r"C:\Windows\WinSxS\amd64_cmd\cmd.exe",     # overlaps on purpose
        ])
        fsindex._merge(mft_like, walked)
        self.index = mft_like
        self.searcher = search.Searcher(mft_like)

    def test_hardlink_name_is_findable(self):
        paths = [r.path for r in self.searcher.search("cmd.exe", 40)]
        self.assertIn(r"C:\Windows\System32\cmd.exe", paths)

    def test_original_name_still_findable(self):
        paths = [r.path for r in self.searcher.search("cmd.exe", 40)]
        self.assertIn(r"C:\Windows\WinSxS\amd64_cmd\cmd.exe", paths)

    def test_duplicate_paths_collapse_to_one_row(self):
        paths = [r.path for r in self.searcher.search("cmd.exe", 40)]
        self.assertEqual(len(paths), len(set(paths)),
                         "an overlapping path must not be listed twice")

    def test_unrelated_entries_unaffected(self):
        paths = [r.path for r in self.searcher.search("notes", 40)]
        self.assertEqual(paths, [r"C:\Users\me\notes.txt"])

    def test_dedup_survives_narrowing(self):
        warm = search.Searcher(self.index)
        for i in range(1, len("cmd.exe") + 1):
            found = [r.path for r in warm.search("cmd.exe"[:i], 40)]
            self.assertEqual(len(found), len(set(found)),
                             f"duplicates appeared at {'cmd.exe'[:i]!r}")


class TestBuildWiring(unittest.TestCase):
    def test_walk_build_ignores_supplement(self):
        # Unprivileged builds already see hardlink names, so no supplement.
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "a.txt"), "w").close()
            with mock.patch.object(fsindex, "is_admin", return_value=False):
                idx = fsindex.build([tmp], (), supplement=(r"C:\Windows",))
            self.assertEqual(idx.source, "walk")
            self.assertIn("a.txt", set(idx.names))

    def test_supplement_is_merged_after_mft(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = os.path.join(tmp, "extra")
            os.makedirs(extra)
            open(os.path.join(extra, "hardlinked.exe"), "w").close()
            fake_mft = make_index([r"C:\only\in\mft.txt"])

            with mock.patch.object(fsindex, "is_admin", return_value=True), \
                 mock.patch.object(fsindex, "build_by_mft", return_value=fake_mft):
                idx = fsindex.build(["C:\\"], (), supplement=(extra,))

            paths = {idx.path(i) for i in range(len(idx))}
            self.assertIn(r"C:\only\in\mft.txt", paths)
            self.assertIn(os.path.join(extra, "hardlinked.exe"), paths)
            self.assertEqual(idx.source, "mft+walk")

    def test_missing_supplement_directory_is_skipped(self):
        fake_mft = make_index([r"C:\only\in\mft.txt"])
        with mock.patch.object(fsindex, "is_admin", return_value=True), \
             mock.patch.object(fsindex, "build_by_mft", return_value=fake_mft):
            idx = fsindex.build(["C:\\"], (), supplement=(r"C:\nope\missing",))
        self.assertIn(r"C:\only\in\mft.txt",
                      {idx.path(i) for i in range(len(idx))})

    def test_empty_supplement_keeps_plain_mft_source(self):
        fake_mft = make_index([r"C:\only\in\mft.txt"])
        with mock.patch.object(fsindex, "is_admin", return_value=True), \
             mock.patch.object(fsindex, "build_by_mft", return_value=fake_mft):
            idx = fsindex.build(["C:\\"], (), supplement=())
        self.assertEqual(idx.source, "mft")


if __name__ == "__main__":
    unittest.main(verbosity=2)
