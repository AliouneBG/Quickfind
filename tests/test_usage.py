"""Frecency: what you opened before, and how much it is allowed to count."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qf import search, usage
from test_search import make_index

HOME = r"C:\Users\me"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "usage.json")
        self.store = usage.UsageStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()


class TestRecording(StoreTest):
    def test_unknown_path_scores_nothing(self):
        self.assertEqual(self.store.boost(r"c:\a\b.txt"), 0.0)

    def test_one_open_gives_a_boost(self):
        self.store.record(r"C:\a\b.txt")
        self.assertGreater(self.store.boost(r"c:\a\b.txt"), 0)

    def test_lookup_is_case_insensitive(self):
        self.store.record(r"C:\A\B.TXT")
        self.assertGreater(self.store.boost(r"c:\a\b.txt"), 0)

    def test_more_opens_score_higher(self):
        self.store.record(r"C:\a\once.txt")
        for _ in range(6):
            self.store.record(r"C:\a\often.txt")
        self.assertGreater(self.store.boost(r"c:\a\often.txt"),
                           self.store.boost(r"c:\a\once.txt"))

    def test_the_boost_is_capped(self):
        for _ in range(500):
            self.store.record(r"C:\a\hot.txt")
        self.assertLessEqual(self.store.boost(r"c:\a\hot.txt"), usage.MAX_BOOST)

    def test_reveal_counts_the_same_as_open(self):
        # Both mean "this is the file I wanted"; only the follow-up differs.
        self.store.record(r"C:\a\opened.txt", usage.OPEN_WEIGHT)
        self.store.record(r"C:\a\revealed.txt", usage.REVEAL_WEIGHT)
        self.assertAlmostEqual(self.store.boost(r"c:\a\opened.txt"),
                               self.store.boost(r"c:\a\revealed.txt"), places=3)

    def test_weights_still_scale_below_one(self):
        # The curve must keep working for fractional weights, in case a weaker
        # signal is ever added back.
        self.store._entries[r"c:\a\weak.txt"] = [0.25, time.time()]
        self.store._entries[r"c:\a\full.txt"] = [1.0, time.time()]
        self.assertLess(self.store.boost(r"c:\a\weak.txt"),
                        self.store.boost(r"c:\a\full.txt"))

    def test_old_entries_decay(self):
        self.store.record(r"C:\a\stale.txt")
        fresh = self.store.boost(r"c:\a\stale.txt")
        entry = self.store._entries[r"c:\a\stale.txt"]
        entry[1] = time.time() - usage.HALF_LIFE_DAYS * 86400
        halved = self.store.boost(r"c:\a\stale.txt")
        self.assertAlmostEqual(halved, fresh / 2, delta=fresh * 0.05)

    def test_recent_single_open_beats_an_ancient_pile(self):
        self.store.record(r"C:\a\recent.txt")
        for _ in range(20):
            self.store.record(r"C:\a\ancient.txt")
        self.store._entries[r"c:\a\ancient.txt"][1] = (
            time.time() - 365 * 86400)
        self.assertGreater(self.store.boost(r"c:\a\recent.txt"),
                           self.store.boost(r"c:\a\ancient.txt"))

    def test_disabled_store_records_nothing(self):
        store = usage.UsageStore(self.path + ".off", enabled=False)
        store.record(r"C:\a\b.txt")
        self.assertEqual(store.boost(r"c:\a\b.txt"), 0.0)
        self.assertEqual(len(store), 0)


class TestPersistence(StoreTest):
    def test_survives_a_reload(self):
        self.store.record(r"C:\a\b.txt")
        reopened = usage.UsageStore(self.path)
        self.assertGreater(reopened.boost(r"c:\a\b.txt"), 0)

    def test_missing_file_is_fine(self):
        self.assertEqual(len(usage.UsageStore(self.path + ".nope")), 0)

    def test_corrupt_file_is_survivable(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(len(usage.UsageStore(self.path)), 0)

    def test_garbage_entries_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "entries": {"a": "nonsense", '
                     '"b": [1, 2]}}')
        store = usage.UsageStore(self.path)
        self.assertEqual(len(store), 1)

    def test_clear_erases_everything(self):
        self.store.record(r"C:\a\b.txt")
        self.store.clear()
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.store.boost(r"c:\a\b.txt"), 0.0)
        self.assertEqual(len(usage.UsageStore(self.path)), 0)

    def test_pruning_keeps_the_valuable_entries(self):
        store = usage.UsageStore(self.path, max_entries=20)
        for n in range(30):
            store.record(rf"C:\cold\file{n}.txt")
        for _ in range(10):
            store.record(r"C:\hot\favourite.txt")
        self.assertLessEqual(len(store), 20)
        self.assertGreater(store.boost(r"c:\hot\favourite.txt"), 0,
                           "the most used path must survive pruning")


class TestRankingWithHistory(unittest.TestCase):
    """History should reorder comparable matches, not conjure new ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = usage.UsageStore(os.path.join(self.tmp.name, "u.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def searcher(self, paths):
        s = search.Searcher(make_index(paths), self.store)
        s._home = HOME.lower() + "\\"
        return s

    def ranked(self, s, query):
        s._last_hits = None
        s._last_primary = ""
        return [r.path for r in s.search(query, 40)]

    def test_an_opened_file_rises_above_its_twins(self):
        paths = [rf"C:\vendor\copy{n}\report.txt" for n in range(6)]
        mine = r"C:\vendor\copy4\report.txt"
        s = self.searcher(paths)
        self.assertNotEqual(self.ranked(s, "report.txt")[0], mine)
        for _ in range(3):
            self.store.record(mine)
        self.assertEqual(self.ranked(s, "report.txt")[0], mine)

    def test_history_does_not_promote_a_non_match(self):
        s = self.searcher([r"C:\a\alpha.txt", r"C:\a\beta.txt"])
        for _ in range(50):
            self.store.record(r"C:\a\beta.txt")
        self.assertEqual(self.ranked(s, "alpha"), [r"C:\a\alpha.txt"])

    def test_history_cannot_beat_a_far_better_match(self):
        # An exact name for a different file still wins over a merely
        # infix-matching file you have opened.
        s = self.searcher([r"C:\a\report.txt", r"C:\b\my_report_draft.txt"])
        for _ in range(50):
            self.store.record(r"C:\b\my_report_draft.txt")
        self.assertEqual(self.ranked(s, "report.txt")[0], r"C:\a\report.txt")

    def test_no_store_behaves_as_before(self):
        plain = search.Searcher(make_index([r"C:\a\report.txt"]))
        self.assertEqual(len(plain.search("report", 40)), 1)

    def test_disabled_store_changes_nothing(self):
        off = usage.UsageStore(os.path.join(self.tmp.name, "off.json"),
                               enabled=False)
        paths = [rf"C:\vendor\copy{n}\report.txt" for n in range(4)]
        s = search.Searcher(make_index(paths), off)
        before = [r.path for r in s.search("report.txt", 40)]
        off.record(r"C:\vendor\copy3\report.txt")
        s._last_hits = None
        s._last_primary = ""
        self.assertEqual([r.path for r in s.search("report.txt", 40)], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
