import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qf import search
from test_search import make_index

SAMPLE = [
    "C:\\Users\\me\\report.txt",
    "C:\\Users\\me\\quarterly_progress_tracker.xlsx",
    "C:\\Users\\me\\Documents\\readme.md",
    "C:\\Users\\me\\Documents\\notes.md",
    "C:\\Windows\\System32\\notepad.exe",
]


class TestFuzzyFallback(unittest.TestCase):
    def setUp(self):
        self.idx = make_index(SAMPLE)
        self.s = search.Searcher(self.idx)

    def names(self, query, **kw):
        return [r.name for r in self.s.search(query, 60, **kw)]

    def test_subsequence_is_found(self):
        self.assertIn("report.txt", self.names("rpt"))

    def test_subsequence_spanning_words(self):
        self.assertIn("quarterly_progress_tracker.xlsx", self.names("qpt"))

    def test_fuzzy_results_are_flagged(self):
        hit = [r for r in self.s.search("rpt", 60) if r.name == "report.txt"][0]
        self.assertTrue(hit.fuzzy)

    def test_substring_results_are_not_flagged(self):
        hit = [r for r in self.s.search("report", 60) if r.name == "report.txt"][0]
        self.assertFalse(hit.fuzzy)

    def test_fuzzy_can_be_disabled(self):
        self.assertEqual(self.s.search("rpt", 60, fuzzy=False), [])

    def test_order_is_not_a_subsequence(self):
        # "tpr" is not in order within "report.txt"
        self.assertNotIn("report.txt", self.names("tpr"))

    def test_fuzzy_never_outranks_substring(self):
        idx = make_index([
            "C:\\a\\notes.md",
            "C:\\a\\n_o_t_e_s_extra.md",
        ])
        s = search.Searcher(idx)
        results = s.search("notes", 60)
        self.assertEqual(results[0].name, "notes.md")
        self.assertFalse(results[0].fuzzy)

    def test_fuzzy_never_outranks_substring_with_extra_tokens(self):
        idx = make_index([
            "C:\\deep\\nested\\folder\\notes.md",
            "C:\\a\\n_o_t_e_s.md",
        ])
        s = search.Searcher(idx)
        results = s.search("notes a", 60)
        substring = [r for r in results if not r.fuzzy]
        fuzzy = [r for r in results if r.fuzzy]
        if substring and fuzzy:
            self.assertGreater(min(r.score for r in substring),
                               max(r.score for r in fuzzy))

    def test_fuzzy_suppressed_when_substring_is_plentiful(self):
        paths = [f"C:\\bulk\\report{n:03d}.txt" for n in range(60)]
        paths.append("C:\\bulk\\r_e_p_o_r_t_spread.txt")
        s = search.Searcher(make_index(paths))
        found = [r for r in s.search("report", 200) if r.fuzzy]
        self.assertEqual(found, [], "fuzzy should stay out of the way")

    def test_short_queries_do_not_trigger_fuzzy(self):
        self.assertEqual([r for r in self.s.search("rp", 60) if r.fuzzy], [])

    def test_tighter_match_scores_higher(self):
        idx = make_index([
            "C:\\a\\rpt.txt",
            "C:\\a\\r_aaaaaaaaaa_p_aaaaaaaaaa_t.txt",
        ])
        s = search.Searcher(idx)
        results = s.search("rpt", 60)
        self.assertEqual(results[0].name, "rpt.txt")

    def test_no_match_still_returns_nothing(self):
        self.assertEqual(self.s.search("zzqqxx", 60), [])

    def test_regex_metacharacters_are_escaped(self):
        idx = make_index(["C:\\a\\file.name.txt", "C:\\a\\other.txt"])
        s = search.Searcher(idx)
        # Must be treated literally, not as a regex wildcard.
        self.assertNotIn("other.txt", [r.name for r in s.search("f.l", 60)])

    def test_fuzzy_does_not_cross_name_boundaries(self):
        idx = make_index(["C:\\a\\abc.txt", "C:\\a\\xyz.txt"])
        s = search.Searcher(idx)
        # "cx" spans the end of one name and the start of the next in the
        # haystack; the NUL guard must prevent a match.
        self.assertEqual([r.name for r in s.search("cxy", 60)], [])

    def test_narrowing_still_matches_cold_search(self):
        warm = search.Searcher(self.idx)
        typed = "report"
        for i in range(1, len(typed) + 1):
            warm_out = [r.path for r in warm.search(typed[:i], 60)]
            cold = [r.path for r in search.Searcher(self.idx).search(typed[:i], 60)]
            self.assertEqual(warm_out, cold, f"diverged at {typed[:i]!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
