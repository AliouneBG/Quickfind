"""Ranking rules, on synthetic indexes so they hold on any machine.

The eval set in evalset.py measures real intent against real files; this file
pins the individual rules that produce those numbers.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qf import search
from test_search import make_index

HOME = r"C:\Users\me"


def searcher_for(paths, home=HOME):
    s = search.Searcher(make_index(paths))
    s._home = home.lower() + "\\"
    return s


def ranked(s, query, limit=40):
    s._last_hits = None
    s._last_primary = ""
    return [r.path for r in s.search(query, limit)]


class TestStemExact(unittest.TestCase):
    """Typing a bare name means the program, not a folder sharing the name."""

    def test_executable_beats_a_folder_of_the_same_name(self):
        s = searcher_for([r"C:\Program Files\Git\cmd\placeholder",
                          r"C:\Windows\System32\cmd.exe"])
        order = ranked(s, "cmd")
        self.assertEqual(order[0], r"C:\Windows\System32\cmd.exe")

    def test_stem_match_beats_a_mere_prefix(self):
        s = searcher_for([r"C:\a\cmd.exe", r"C:\a\cmdkey.exe"])
        self.assertEqual(ranked(s, "cmd")[0], r"C:\a\cmd.exe")

    def test_a_known_file_type_outranks_a_bare_exact_name(self):
        # Same rule that makes "cmd" mean cmd.exe: a stem match plus a useful
        # extension beats an extensionless file that happens to match exactly.
        s = searcher_for([r"C:\a\report", r"C:\a\report.txt"])
        self.assertEqual(ranked(s, "report")[0], r"C:\a\report.txt")

    def test_an_unknown_extension_does_not_outrank_the_exact_name(self):
        s = searcher_for([r"C:\a\report", r"C:\a\report.qqq"])
        self.assertEqual(ranked(s, "report")[0], r"C:\a\report")

    def test_no_stem_boost_when_the_query_has_an_extension(self):
        # "cmd.exe" is an explicit filename; the folder must not be promoted.
        s = searcher_for([r"C:\a\cmd.exe", r"C:\b\cmd\inner.txt"])
        self.assertEqual(ranked(s, "cmd.exe")[0], r"C:\a\cmd.exe")

    def test_partial_stem_is_not_an_exact_hit(self):
        s = searcher_for([r"C:\a\notes_1.txt", r"C:\a\notes.txt"])
        self.assertEqual(ranked(s, "notes")[0], r"C:\a\notes.txt")


class TestHomePrior(unittest.TestCase):
    def test_your_file_beats_another_profile(self):
        s = searcher_for([r"C:\Users\Public\Downloads\notes.txt",
                          r"C:\Users\me\Downloads\notes.txt"])
        self.assertEqual(ranked(s, "notes.txt")[0],
                         r"C:\Users\me\Downloads\notes.txt")

    def test_your_folder_beats_a_system_folder(self):
        s = searcher_for([r"C:\Users\Default\Desktop\x",
                          r"C:\Users\me\Desktop\x"])
        order = ranked(s, "desktop")
        self.assertTrue(order[0].startswith(r"C:\Users\me"), order[:2])

    def test_plain_appdata_gets_no_bonus(self):
        s = searcher_for([r"C:\Users\me\AppData\Roaming\thing\report.txt",
                          r"C:\Users\me\Documents\report.txt"])
        self.assertEqual(ranked(s, "report.txt")[0],
                         r"C:\Users\me\Documents\report.txt")

    def test_user_installed_programs_count_as_home(self):
        # Python and VS Code install here; it is not the same as Roaming junk.
        # Equal depth on both sides, so only the bonus can decide.
        s = searcher_for([
            r"C:\Users\me\AppData\Roaming\vendor\python.exe",
            r"C:\Users\me\AppData\Local\Programs\python.exe",
        ])
        self.assertEqual(ranked(s, "python")[0],
                         r"C:\Users\me\AppData\Local\Programs\python.exe")


class TestDemotedLocations(unittest.TestCase):
    def test_system32_beats_syswow64(self):
        s = searcher_for([r"C:\Windows\SysWOW64\cmd.exe",
                          r"C:\Windows\System32\cmd.exe"])
        self.assertEqual(ranked(s, "cmd")[0], r"C:\Windows\System32\cmd.exe")

    def test_shell_shortcut_store_is_demoted(self):
        s = searcher_for([r"C:\Users\me\Links\Downloads.lnk",
                          r"C:\Users\me\Downloads\placeholder"])
        order = ranked(s, "downloads")
        self.assertEqual(order[0], r"C:\Users\me\Downloads")

    def test_noise_paths_still_demoted(self):
        s = searcher_for([r"C:\Users\me\project\node_modules\pkg\index.js",
                          r"C:\Users\me\project\src\index.js"])
        self.assertEqual(ranked(s, "index.js")[0],
                         r"C:\Users\me\project\src\index.js")

    def test_demotions_stack(self):
        s = searcher_for([r"C:\Windows\SysWOW64\thing.exe"])
        plain = searcher_for([r"C:\Windows\System32\thing.exe"])
        mirrored = s.search("thing", 5)[0].score
        canonical = plain.search("thing", 5)[0].score
        self.assertLess(mirrored, canonical)


class TestSpreading(unittest.TestCase):
    """Ten copies of one filename must not fill the list."""

    def setUp(self):
        self.paths = [rf"C:\vendor\copy{n}\shared.txt" for n in range(8)]
        self.paths.append(r"C:\Users\me\notes\shared_notes.txt")

    def test_one_name_cannot_claim_every_visible_row(self):
        s = searcher_for(self.paths)
        names = [r.name.lower() for r in s.search("shared", 40)]
        front = names[:search.DUPLICATE_QUOTA + 1]
        self.assertLess(front.count("shared.txt"), len(front),
                        "a distinct name must get a slot near the top")

    def test_deferred_copies_are_still_reachable(self):
        # Deferring, not penalising: every copy stays in the result set.
        s = searcher_for(self.paths)
        found = [r.path for r in s.search("shared.txt", 40)]
        self.assertEqual(len(found), 8)

    def test_a_distinct_name_rises_above_the_repeats(self):
        s = searcher_for(self.paths)
        order = ranked(s, "shared")
        distinct = r"C:\Users\me\notes\shared_notes.txt"
        self.assertIn(distinct, order)
        self.assertLess(order.index(distinct), len(order) - 1,
                        "a unique name should not be last behind 8 clones")

    def test_the_best_copy_still_comes_first(self):
        s = searcher_for([r"C:\vendor\deep\down\here\shared.txt",
                          r"C:\Users\me\shared.txt"])
        self.assertEqual(ranked(s, "shared.txt")[0], r"C:\Users\me\shared.txt")

    def test_spreading_never_reorders_distinct_names(self):
        s = searcher_for([r"C:\Users\me\alpha.txt", r"C:\Users\me\alphabet.txt"])
        order = ranked(s, "alpha")
        self.assertEqual(order[0], r"C:\Users\me\alpha.txt")

    def test_single_results_are_untouched(self):
        s = searcher_for([r"C:\Users\me\unique.txt"])
        self.assertEqual(len(ranked(s, "unique")), 1)


class TestNoRegressions(unittest.TestCase):
    def test_fuzzy_still_ranks_below_substring(self):
        s = searcher_for([r"C:\Users\me\notes.md", r"C:\Users\me\n_o_t_e_s.md"])
        results = s.search("notes", 40)
        self.assertFalse(results[0].fuzzy)

    def test_multi_token_still_filters_by_path(self):
        s = searcher_for([r"C:\Users\me\alpha\report.txt",
                          r"C:\Users\me\beta\report.txt"])
        self.assertEqual(ranked(s, "report beta"),
                         [r"C:\Users\me\beta\report.txt"])

    def test_copies_of_one_name_stay_in_score_order(self):
        # Spreading interleaves names, so global score order is no longer the
        # contract. Within a single name the best copy must still come first.
        s = searcher_for([rf"C:\Users\me\dir{n}\report.txt" for n in range(6)])
        scores = [r.score for r in s.search("report", 40)
                  if r.name.lower() == "report.txt"]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestInstallersAndLaunchers(unittest.TestCase):
    """Searching for a program should not lead with the thing that installed it.

    Typing a media player's name returned its installer, its logo and its
    playlist above the program itself, and the rows all looked alike.
    """

    PATHS = [
        r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"C:\Users\me\Downloads\PotPlayerSetup64.exe",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\PotPlayer\PotPlayer 64 bit.lnk",
    ]

    def test_the_installer_is_not_the_first_answer(self):
        order = ranked(searcher_for(self.PATHS), "potplayer")
        self.assertNotIn("Setup", order[0])

    def test_the_installer_still_ranks(self):
        # Demoted, not hidden: it is a real file that really matched.
        order = ranked(searcher_for(self.PATHS), "potplayer")
        self.assertIn(r"C:\Users\me\Downloads\PotPlayerSetup64.exe", order)

    def test_asking_for_the_installer_finds_it_first(self):
        order = ranked(searcher_for(self.PATHS), "potplayer setup")
        self.assertEqual(order[0],
                         r"C:\Users\me\Downloads\PotPlayerSetup64.exe")

    def test_the_start_menu_entry_beats_a_copy_elsewhere(self):
        paths = [
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Thing\Thing.lnk",
            r"C:\Users\me\Documents\backups\Thing.lnk",
        ]
        order = ranked(searcher_for(paths), "thing")
        self.assertIn("Start Menu", order[0])

    def test_an_uninstaller_is_not_promoted_by_being_in_the_start_menu(self):
        paths = [
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Thing\Uninstall Thing.lnk",
            r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Thing\Thing.lnk",
        ]
        order = ranked(searcher_for(paths), "thing")
        self.assertNotIn("Uninstall", order[0])

    def test_a_folder_named_setup_is_not_an_installer(self):
        s = searcher_for([r"C:\Users\me\projects\setup\notes.txt"])
        self.assertTrue(ranked(s, "setup"))
