import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qf import fsindex, search


def make_index(paths):
    """Build an Index from ``C:\\a\\b.txt`` style strings."""
    idx = fsindex.Index()
    idx.source = "test"
    lookup = {}
    for path in paths:
        parts = path.split("\\")
        parent = -1
        prefix = ""
        for depth, part in enumerate(parts):
            prefix = prefix + "\\" + part if prefix else part
            if prefix in lookup:
                parent = lookup[prefix]
                continue
            is_dir = depth < len(parts) - 1
            i = idx.add(part, parent, is_dir)
            lookup[prefix] = i
            if parent == -1:
                idx.roots[i] = part
            parent = i
    return idx


SAMPLE = [
    "C:\\Users\\me\\report.txt",
    "C:\\Users\\me\\reports\\annual-report.pdf",
    "C:\\Users\\me\\reports\\draft_report.docx",
    "C:\\Users\\me\\Documents\\quarterly report.xlsx",
    "C:\\Users\\me\\Documents\\myreportfile.txt",
    "C:\\Users\\me\\Downloads\\report.txt",
    "C:\\Users\\me\\projects\\node_modules\\reporter\\index.js",
    "C:\\Windows\\System32\\notepad.exe",
    "C:\\Users\\me\\notes.md",
]


class TestIndex(unittest.TestCase):
    def test_path_reconstruction(self):
        idx = make_index(SAMPLE)
        rebuilt = {idx.path(i) for i in range(len(idx)) if not idx.isdir[i]}
        self.assertEqual(rebuilt, set(SAMPLE))

    def test_depth(self):
        idx = make_index(["C:\\Users\\me\\report.txt"])
        by_name = {idx.names[i]: i for i in range(len(idx))}
        self.assertEqual(idx.depth(by_name["C:"]), 0)
        self.assertEqual(idx.depth(by_name["Users"]), 1)
        self.assertEqual(idx.depth(by_name["report.txt"]), 3)

    def test_depth_with_forward_parent(self):
        # MFT order can list a child before its parent.
        idx = fsindex.Index()
        child = idx.add("child.txt", 2, False)
        mid = idx.add("mid", 2, True)
        root = idx.add("C:", -1, True)
        idx.parents[child] = mid
        idx.roots[root] = "C:"
        self.assertEqual(idx.depth(root), 0)
        self.assertEqual(idx.depth(mid), 1)
        self.assertEqual(idx.depth(child), 2)


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.idx = make_index(SAMPLE)
        self.s = search.Searcher(self.idx)

    def names(self, query, limit=60):
        return [r.name for r in self.s.search(query, limit)]

    def test_empty_query(self):
        self.assertEqual(self.s.search(""), [])
        self.assertEqual(self.s.search("   "), [])

    def test_basic_substring(self):
        self.assertIn("notepad.exe", self.names("notepad"))

    def test_case_insensitive(self):
        self.assertEqual(self.names("NOTEPAD"), self.names("notepad"))

    def test_no_match(self):
        self.assertEqual(self.s.search("zzzzznotathing"), [])

    def test_exact_name_ranks_first(self):
        self.assertEqual(self.names("report.txt")[0], "report.txt")

    def test_prefix_beats_infix(self):
        result = self.names("report")
        self.assertLess(result.index("report.txt"), result.index("annual-report.pdf"))

    def test_boundary_beats_plain_infix(self):
        scores = {r.name: r.score for r in self.s.search("report")}
        self.assertGreater(scores["annual-report.pdf"], scores["myreportfile.txt"])

    def test_entry_reported_once_per_query(self):
        idx = make_index(["C:\\a\\aaa.txt", "C:\\a\\plain.txt"])
        s = search.Searcher(idx)
        found = [r.name for r in s.search("a")]
        self.assertEqual(len(found), len(set(found)))
        self.assertEqual(found.count("aaa.txt"), 1)

    def test_noise_paths_demoted(self):
        result = self.names("report")
        self.assertIn("reporter", result)
        self.assertGreater(result.index("reporter"), result.index("report.txt"))

    def test_multi_token_first_in_name_rest_in_path(self):
        result = self.names("report downloads")
        self.assertEqual(result, ["report.txt"])
        self.assertEqual(self.s.search("report downloads")[0].path,
                         "C:\\Users\\me\\Downloads\\report.txt")

    def test_multi_token_no_match(self):
        self.assertEqual(self.s.search("report nosuchfolder"), [])

    def test_second_token_does_not_match_name_only(self):
        # "notepad" is not in any path alongside a name containing "report"
        self.assertEqual(self.s.search("report notepad"), [])

    def test_directories_are_searchable(self):
        self.assertIn("reports", self.names("reports"))

    def test_limit_respected(self):
        self.assertLessEqual(len(self.s.search("e", limit=3)), 3)

    def test_results_sorted_by_score(self):
        results = self.s.search("report")
        self.assertEqual([r.score for r in results],
                         sorted((r.score for r in results), reverse=True))


class TestIncrementalNarrowing(unittest.TestCase):
    """Typing must never change the answer versus a cold search."""

    def setUp(self):
        self.idx = make_index(SAMPLE)

    def assert_same_as_cold(self, typed):
        warm = search.Searcher(self.idx)
        for i in range(1, len(typed) + 1):
            warm_result = warm.search(typed[:i])
            cold = search.Searcher(self.idx)
            cold_result = cold.search(typed[:i])
            self.assertEqual(
                [(r.path, round(r.score, 6)) for r in warm_result],
                [(r.path, round(r.score, 6)) for r in cold_result],
                f"divergence after typing {typed[:i]!r}",
            )

    def test_single_token(self):
        self.assert_same_as_cold("report.txt")

    def test_growing_into_multi_token(self):
        self.assert_same_as_cold("report downloads")

    def test_token_extends_after_space(self):
        self.assert_same_as_cold("re ann")

    def test_backspacing_rescans(self):
        s = search.Searcher(self.idx)
        s.search("report")
        widened = s.search("re")
        cold = search.Searcher(self.idx).search("re")
        self.assertEqual([r.path for r in widened], [r.path for r in cold])

    def test_unrelated_query_after_narrowing(self):
        s = search.Searcher(self.idx)
        s.search("report")
        self.assertIn("notepad.exe", [r.name for r in s.search("notepad")])


class TestHaystackOffsets(unittest.TestCase):
    def test_unicode_lowering_changes_length(self):
        # "\u0130".lower() is two codepoints; offsets must track the lowered form.
        idx = make_index([
            "C:\\a\\before.txt",
            "C:\\a\\\u0130stanbul.txt",
            "C:\\a\\after.txt",
        ])
        s = search.Searcher(idx)
        self.assertEqual([r.name for r in s.search("after")], ["after.txt"])
        self.assertEqual([r.name for r in s.search("before")], ["before.txt"])
        hit = s.search("stanbul")
        self.assertEqual([r.name for r in hit], ["\u0130stanbul.txt"])

    def test_match_never_spans_two_names(self):
        idx = make_index(["C:\\a\\ab.txt", "C:\\a\\cd.txt"])
        s = search.Searcher(idx)
        self.assertEqual(s.search("txtcd"), [])


class TestScanCap(unittest.TestCase):
    def test_cap_does_not_poison_narrowed_results(self):
        original = search.MAX_SCAN_HITS
        search.MAX_SCAN_HITS = 5
        try:
            paths = [f"C:\\bulk\\item{n:04d}.txt" for n in range(60)]
            paths.append("C:\\bulk\\item_special_target.txt")
            idx = make_index(paths)
            s = search.Searcher(idx)
            s.search("item")  # capped, must not be cached
            self.assertIsNone(s._last_hits)
            narrowed = [r.name for r in s.search("item_special")]
            self.assertEqual(narrowed, ["item_special_target.txt"])
        finally:
            search.MAX_SCAN_HITS = original


class TestWalkerAndCache(unittest.TestCase):
    def test_walk_temp_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub", "deeper"))
            open(os.path.join(tmp, "top.txt"), "w").close()
            open(os.path.join(tmp, "sub", "deeper", "buried.log"), "w").close()

            idx = fsindex.build_by_walk([tmp])
            found = {idx.names[i]: idx.path(i) for i in range(len(idx))}
            self.assertIn("top.txt", found)
            self.assertIn("buried.log", found)
            self.assertTrue(found["buried.log"].endswith(
                os.path.join("sub", "deeper", "buried.log")))

            s = search.Searcher(idx)
            self.assertEqual([r.name for r in s.search("buried")], ["buried.log"])

    def test_walk_skips_excluded_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "winsxs", "inner"))
            open(os.path.join(tmp, "winsxs", "inner", "hidden.txt"), "w").close()
            idx = fsindex.build_by_walk([tmp], excludes=("winsxs",))
            self.assertNotIn("hidden.txt", idx.names)

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx = make_index(SAMPLE)
            target = os.path.join(tmp, "index.pkl")
            fsindex.save(idx, target)
            loaded = fsindex.load(target)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.names, idx.names)
            self.assertEqual(list(loaded.parents), list(idx.parents))
            self.assertEqual(
                [r.path for r in search.Searcher(loaded).search("report")],
                [r.path for r in search.Searcher(idx).search("report")],
            )

    def test_cache_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "index.pkl")
            fsindex.save(make_index(SAMPLE), target)
            import pickle
            with open(target, "rb") as fh:
                payload = pickle.load(fh)
            payload["version"] = 0
            with open(target, "wb") as fh:
                pickle.dump(payload, fh)
            self.assertIsNone(fsindex.load(target))

    def test_cache_missing_file(self):
        self.assertIsNone(fsindex.load(os.path.join(tempfile.gettempdir(), "nope.pkl")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
