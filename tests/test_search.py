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


class TestShortLeadingWord(unittest.TestCase):
    """A short first word must not swallow the whole scan.

    The first token is the one scanned against the name haystack, and a one
    or two character token caps at the first few thousand hits in index
    order. That slice is arbitrary, so the words that would actually identify
    the file only got to filter whatever the short word happened to collect.
    Reported from a real search: typing the full, exact name of a file
    beginning "A Night in ..." returned nothing at all.
    """

    def setUp(self):
        # Enough decoys containing "a" to cap the scan for real, with the
        # wanted file last so an index-order slice cannot include it.
        paths = [rf"C:\noise\padding{n:05d}a.txt"
                 for n in range(search.MAX_SCAN_HITS + 200)]
        paths.append(r"C:\music\A Night in Mondstadt.flac")
        self.searcher = search.Searcher(make_index(paths))
        self.want = r"c:\music\a night in mondstadt.flac"

    def rank(self, query):
        self.searcher._last_hits = None
        self.searcher._last_primary = ""
        rows = self.searcher.search(query, 200, fuzzy=False)
        for n, row in enumerate(rows, 1):
            if row.path.lower() == self.want:
                return n
        return None

    def test_the_exact_name_finds_the_file(self):
        self.assertEqual(self.rank("a night in mondstadt"), 1)

    def test_a_short_first_word_still_finds_it(self):
        self.assertIsNotNone(self.rank("a night"))

    def test_either_order_works(self):
        self.assertIsNotNone(self.rank("night a"))
        self.assertIsNotNone(self.rank("a mondstadt"))

    def test_a_specific_first_word_is_left_alone(self):
        # "mondstadt" does not cap, so nothing is promoted and the ordinary
        # reading -- first word names the file, the rest filter the path --
        # is untouched.
        self.assertEqual(self.rank("mondstadt night"), 1)

    def test_a_repeated_word_still_filters(self):
        # Only the promoted occurrence leaves `rest`; the other must remain.
        self.assertIsNotNone(self.rank("a night a"))

    def test_a_word_that_is_not_there_still_excludes(self):
        # Promotion must not turn the other words into decoration.
        self.assertIsNone(self.rank("a night liyue"))


class TestInitialsMatching(unittest.TestCase):
    """People shorten the names of things they use every day.

    "vs code" for Visual Studio Code, "ms word" for Microsoft Word. Nothing in
    "Visual Studio Code" contains the substring "vs", so no amount of ranking
    reaches it -- the file was not scored low, it was never a candidate.
    """

    def setUp(self):
        self.paths = [
            r"C:\Users\me\AppData\Local\Programs\Visual Studio Code.lnk",
            r"C:\Users\me\Documents\code notes.txt",
            r"C:\Users\me\Projects\VeryStrangeCode.txt",
            r"C:\Windows\System32\notepad.exe",
        ]
        self.searcher = search.Searcher(make_index(self.paths))

    def ranked(self, query, limit=40):
        self.searcher._last_hits = None
        self.searcher._last_primary = ""
        return [r.path for r in self.searcher.search(query, limit)]

    def test_initials_find_what_no_substring_could(self):
        rows = self.ranked("vs code")
        self.assertIn(self.paths[0], rows)

    def test_spelling_it_out_still_works(self):
        self.assertIn(self.paths[0], self.ranked("visual code"))
        self.assertIn(self.paths[0], self.ranked("studio code"))

    def test_a_name_that_says_it_outright_comes_first(self):
        # The caution that matters: initials are a weaker signal than the
        # words being there, so they must not outrank them.
        rows = self.ranked("code notes")
        self.assertEqual(rows[0], self.paths[1])

    def test_initials_do_not_match_the_middle_of_a_name(self):
        # "sc" are initials two and three of Visual Studio Code, not one and
        # two, so this must not match it.
        self.assertNotIn(self.paths[0], self.ranked("sc code"))

    def test_a_word_that_is_absent_still_excludes(self):
        self.assertNotIn(self.paths[0], self.ranked("vs code python"))

    def test_camel_case_reads_as_words(self):
        self.assertIn(self.paths[2], self.ranked("vs code"))


class TestInitialsOfAName(unittest.TestCase):
    def test_spaces_and_separators(self):
        self.assertEqual(search._initials("Visual Studio Code.lnk"), "vsc")
        self.assertEqual(search._initials("my_report_final.docx"), "mrf")
        self.assertEqual(search._initials("Google Chrome.lnk"), "gc")

    def test_camel_humps(self):
        self.assertEqual(search._initials("VisualStudioCode.exe"), "vsc")

    def test_the_extension_is_not_a_word(self):
        self.assertEqual(search._initials("notepad.exe"), "n")

    def test_a_leading_dot_is_part_of_the_name(self):
        self.assertEqual(search._initials(".gitignore"), "g")


class TestTypedNotPasted(unittest.TestCase):
    """Queries arrive a character at a time, and that is a different path.

    Every keystroke extends the previous one, so the refinement cache hits and
    the haystack is never rescanned. Anything done only on a cache miss is
    therefore done only when a query is issued cold -- which is how tests call
    it and not how anybody types. The second scan behind "vs code" lived in
    that branch and did nothing in the real launcher for exactly that reason.
    """

    def setUp(self):
        self.paths = [
            r"C:\Users\me\AppData\Local\Programs\Visual Studio Code.lnk",
            r"C:\Users\me\Projects\vscode.proposed.d.ts",
            r"C:\Users\me\Projects\vscode.notes.txt",
            r"C:\Windows\System32\notepad.exe",
        ]
        self.index = make_index(self.paths)

    def typed(self, query, limit=40):
        """Search the way the window does: one character at a time."""
        searcher = search.Searcher(self.index)
        rows = []
        for cut in range(1, len(query) + 1):
            rows = searcher.search(query[:cut], limit)
        return [r.path for r in rows]

    def pasted(self, query, limit=40):
        searcher = search.Searcher(self.index)
        return [r.path for r in searcher.search(query, limit)]

    def test_initials_work_when_the_query_is_typed(self):
        self.assertIn(self.paths[0], self.typed("vs code"))

    def test_typing_and_pasting_agree(self):
        for query in ("vs code", "vscode notes", "notepad"):
            with self.subTest(query=query):
                self.assertEqual(set(self.typed(query)), set(self.pasted(query)))


class TestProgramAliases(unittest.TestCase):
    """People type what they call a program, not what it is called on disk.

    "vscode" is not an initialism -- that would be "vsc" -- and it is not a
    substring of "Visual Studio Code" either. It is the first two words
    shortened to their initials with the last one left whole, which is how
    these are actually built: VS + Code.
    """

    def setUp(self):
        self.paths = [
            r"C:\Users\me\AppData\Roaming\Start Menu\Programs\Visual Studio Code.lnk",
            r"C:\Users\me\AppData\Local\Programs\VS Code\policies\VSCode.admx",
            r"C:\Users\me\Notes\vscode tips.txt",
            r"C:\Program Files\Google\Google Chrome.lnk",
        ]
        self.index = make_index(self.paths)

    def typed(self, query, limit=40):
        searcher = search.Searcher(self.index)
        rows = []
        for cut in range(1, len(query) + 1):
            rows = searcher.search(query[:cut], limit)
        return [r.path for r in rows]

    def test_the_collapsed_form_finds_the_program(self):
        self.assertEqual(self.typed("vscode")[0], self.paths[0])

    def test_so_does_the_spaced_form(self):
        self.assertEqual(self.typed("vs code")[0], self.paths[0])

    def test_and_the_bare_initials(self):
        self.assertEqual(self.typed("vsc")[0], self.paths[0])

    def test_the_whole_name_run_together(self):
        self.assertEqual(self.typed("googlechrome")[0], self.paths[3])

    def test_a_program_beats_a_file_that_merely_spells_it(self):
        # VSCode.admx is an exact stem match for "vscode" and still must not
        # win: a policy template is not what anybody means by "vscode".
        rows = self.typed("vscode")
        self.assertLess(rows.index(self.paths[0]), rows.index(self.paths[1]))

    def test_only_programs_are_aliased(self):
        # A text file called "vscode tips" keeps its ordinary substring match
        # and gains no alias of its own.
        forms = search._alias_forms("vscode tips.txt")
        self.assertIn("vscodetips", forms)
        searcher = search.Searcher(self.index)
        self.assertNotIn("vt", searcher._aliases)

    def test_an_alias_is_an_exact_lookup_not_a_substring(self):
        # "vscod" is a prefix of the alias and must not match through it.
        searcher = search.Searcher(self.index)
        self.assertIn("vscode", searcher._aliases)
        self.assertNotIn("vscod", searcher._aliases)

    def test_single_letters_are_not_aliases(self):
        # Every name would otherwise answer to one letter.
        self.assertNotIn("v", search._alias_forms("Visual Studio Code.lnk"))
        self.assertNotIn("n", search._alias_forms("notepad.exe"))


class TestWordsRunTogether(unittest.TestCase):
    """People do not put the spaces where the file does.

    A boarding pass called "Find Your Trip_ Delta Air Lines.pdf" was not found
    by "delta airlines": the file says "Air Lines" and the query says
    "airlines", and neither is a substring of the other anywhere in the path.
    """

    def setUp(self):
        self.paths = [
            r"C:\Users\me\Desktop\Find Your Trip_ Delta Air Lines.pdf",
            r"C:\Users\me\Desktop\United Airlines booking.pdf",
            r"C:\Users\me\Desktop\notes.txt",
        ]
        self.index = make_index(self.paths)

    def typed(self, query, limit=40):
        searcher = search.Searcher(self.index)
        rows = []
        for cut in range(1, len(query) + 1):
            rows = searcher.search(query[:cut], limit)
        return [r.path for r in rows]

    def test_a_word_split_in_the_name_still_matches(self):
        self.assertIn(self.paths[0], self.typed("delta airlines"))

    def test_the_spaced_form_still_matches(self):
        self.assertIn(self.paths[0], self.typed("delta air"))

    def test_it_does_not_match_across_unrelated_files(self):
        rows = self.typed("delta airlines")
        self.assertNotIn(self.paths[1], rows)
        self.assertNotIn(self.paths[2], rows)

    def test_squashing_keeps_only_letters_and_digits(self):
        self.assertEqual(search._squashed("Find Your Trip_ Delta Air Lines.pdf"),
                         "findyourtripdeltaairlinespdf")
        self.assertEqual(search._squashed("report-2024_final.docx"),
                         "report2024finaldocx")


class TestVendoredCodeIsDemoted(unittest.TestCase):
    r"""Somebody else's source, shipped inside something you installed.

    Measured across nineteen everyday queries these were 28% of the top ten
    results, and typing "delta" returned eight of them against one real file.
    They are neither system files nor yours, which is why looking only at
    C:\Windows found almost nothing wrong.
    """

    def setUp(self):
        self.mine = r"C:\Users\me\Desktop\Find Your Trip_ Delta Air Lines.pdf"
        self.stub = (r"C:\Users\me\.vscode\extensions\pylance\dist"
                     r"\bundled\stubs\sympy-stubs\delta.pyi")
        searcher = search.Searcher(make_index([self.mine, self.stub]))
        searcher._home = r"c:\users\me" + "\\"
        self.searcher = searcher

    def ranked(self, query, limit=40):
        self.searcher._last_hits = None
        self.searcher._last_primary = ""
        return [r.path for r in self.searcher.search(query, limit)]

    def test_your_own_file_beats_a_bundled_stub(self):
        # delta.pyi is an exact stem match for "delta", worth far more on name
        # alone; being vendored is what has to outweigh that.
        rows = self.ranked("delta")
        self.assertLess(rows.index(self.mine), rows.index(self.stub))

    def test_the_stub_is_demoted_not_hidden(self):
        self.assertIn(self.stub, self.ranked("delta"))

    def test_the_directories_that_were_missing_are_covered(self):
        for marker in (r"\bundled\stubs" + "\\", r"\vendor_perl" + "\\",
                       r"\typeshed-fallback" + "\\",
                       r"\.vscode\extensions" + "\\"):
            with self.subTest(marker=marker):
                self.assertIn(marker, search.NOISE)
