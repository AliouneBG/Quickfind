"""Telling the user how much they are not seeing.

"40 shown" implied 40 was all there was. On a real machine "resume" matches 535
files, and nothing hinted that a second word would cut that to one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import quickfind
from qf import search
from test_search import make_index


def searcher_for(paths):
    return search.Searcher(make_index(paths))


class TestMatchTotals(unittest.TestCase):
    def setUp(self):
        self.paths = [rf"C:\bucket\note{n:03d}.txt" for n in range(60)]
        self.paths.append(r"C:\other\unrelated.dat")
        self.s = searcher_for(self.paths)

    def test_total_counts_every_name_match(self):
        self.s.search("note", 10)
        self.assertEqual(self.s.last_total, 60)

    def test_total_is_independent_of_the_limit(self):
        self.s.search("note", 5)
        five = self.s.last_total
        self.s._last_hits = None
        self.s._last_primary = ""
        self.s.search("note", 40)
        self.assertEqual(five, self.s.last_total)

    def test_multi_word_reports_no_total(self):
        # The name-match count is not the result count once a path filter
        # applies, so reporting it would mislead.
        self.s.search("note other", 10)
        self.assertIsNone(self.s.last_total)

    def test_empty_query_clears_the_total(self):
        self.s.search("note", 10)
        self.s.search("", 10)
        self.assertIsNone(self.s.last_total)

    def test_capped_scan_is_flagged(self):
        original = search.MAX_SCAN_HITS
        search.MAX_SCAN_HITS = 5
        try:
            s = searcher_for(self.paths)
            s.search("note", 10)
            self.assertTrue(s.last_total_capped)
        finally:
            search.MAX_SCAN_HITS = original

    def test_uncapped_scan_is_not_flagged(self):
        self.s.search("note", 10)
        self.assertFalse(self.s.last_total_capped)


class TestStatusLine(unittest.TestCase):
    """The Controller turns those totals into something readable."""

    def build(self, paths, query, limit=10):
        searcher = searcher_for(paths)

        class Stub:
            cfg = {"max_results": limit, "fuzzy": True}
            _indexing = False
            _scanned = 0
            # Nothing has arrived since the index was built, but the real
            # methods run so the status line is exercised as it ships.
            fresh_searcher = None
            _fresh_dirty = False
            _refresh_fresh = quickfind.Controller._refresh_fresh
            _with_fresh = quickfind.Controller._with_fresh

        stub = Stub()
        stub.searcher = searcher
        return quickfind.Controller.query(stub, query)

    def test_reports_the_share_you_are_seeing(self):
        paths = [rf"C:\bucket\note{n:03d}.txt" for n in range(60)]
        _results, note = self.build(paths, "note")
        self.assertIn("10 of 60", note)

    def test_suggests_narrowing_when_truncated(self):
        paths = [rf"C:\bucket\note{n:03d}.txt" for n in range(60)]
        _results, note = self.build(paths, "note")
        self.assertIn("Add a word to narrow", note)

    def test_no_nagging_when_everything_fits(self):
        _results, note = self.build([r"C:\a\solo.txt"], "solo")
        self.assertIn("1 shown", note)
        self.assertNotIn("Add a word", note)

    def test_thousands_are_grouped(self):
        paths = [rf"C:\bulk\item{n:05d}.txt" for n in range(1500)]
        _results, note = self.build(paths, "item")
        self.assertIn("1,500", note)

    def test_capped_total_is_marked_approximate(self):
        original = search.MAX_SCAN_HITS
        search.MAX_SCAN_HITS = 20
        try:
            paths = [rf"C:\bulk\item{n:05d}.txt" for n in range(100)]
            _results, note = self.build(paths, "item")
            self.assertIn("+", note)
        finally:
            search.MAX_SCAN_HITS = original

    def test_keys_are_still_advertised(self):
        _results, note = self.build([r"C:\a\solo.txt"], "solo")
        self.assertIn("Enter opens", note)
        self.assertIn("Ctrl+Enter reveals", note)

    def test_no_matches_says_so(self):
        _results, note = self.build([r"C:\a\solo.txt"], "zzznothing")
        self.assertIn("No matches", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
