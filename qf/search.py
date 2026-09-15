"""Substring search over an Index, tuned for per-keystroke latency.

What keeps this responsive on ~1.4M entries:

1. Every lowercase name is packed into one NUL-separated UTF-8 ``bytes``
   haystack, so scanning is ``bytes.find`` in a loop -- C speed rather than a
   Python loop over a million strings. Scoring reads slices of that same
   buffer, so it never decodes or re-lowercases a candidate.
2. Candidates are carried as *positions*, not entry indexes. Turning a position
   into an index is a binary search over a 1.4M-element array, and doing it per
   hit was the largest single cost in a query. Scoring only needs the name,
   which a position already gives, so the lookup is deferred to the few hundred
   results that actually reach the pool.
3. Typing narrows. Candidates for a first token are always a subset of the
   candidates for any prefix of it, so an extending query re-filters the
   previous list instead of rescanning.
4. Short queries scan less. A one-character query has no meaningful ranking to
   offer, so its candidate pool is capped far lower than a specific one's.
5. Full paths are resolved lazily, in descending score order, only until the
   result pool is full.

Match semantics: the first token must appear in the entry's *name*; any further
tokens may appear anywhere in its full path. That is what makes rule 3 sound --
extending a query can only ever tighten the first token's constraint.

When a substring search comes back nearly empty, a subsequence ("fuzzy") pass
runs as a fallback so ``rpt`` still finds ``report.txt``. Fuzzy hits are scored
strictly below every substring hit, so they only ever appear underneath.
"""
from __future__ import annotations

import array
import os
import re
import time
from bisect import bisect_right

from . import kinds

MAX_SCAN_HITS = 12000
MAX_PATH_PROBES = 6000
# The most matches that will ever be turned into results. Resolving a path is
# the cost here, and the list is a viewport that draws a dozen rows however
# long it is, so this is what the user can scroll through.
REFINE_POOL = 2000
# The smallest pool that still gives `_spread` room to interleave repeated
# names, whatever limit the caller asked for.
SPREAD_POOL = 400

# A word this short in a multi-word query is read as possible initials.
INITIALISM_MAX = 3
# Programs are aliased by the names people call them. Only programs: this is
# the set where "I type what I call it" applies, and on the development
# machine it is 3,431 entries out of 771,070, so it costs nothing. Doing it
# for every file would put a second name on a million things nobody calls
# anything.
ALIAS_EXTENSIONS = (".exe", ".lnk")
ALIAS_MAX_WORDS = 8
ALIAS_MIN_LEN = 2
# Typing what a program is called is an exact match, just against its
# colloquial name rather than its filename, so it sits in the same tier as one
# -- and what the file *is* decides between them, through the extension bonus
# both of them carry. That is what separates Visual Studio Code from
# VSCode.admx, a policy template that is an exact stem match for "vscode" and
# collects full marks for recency every time the editor updates itself.
ALIAS_SCORE = 980.0
# Where a word matched on initials ranks: below anything written out in the
# name, above the subsequence fallback.
ABBREV_CEILING = 160.0
FUZZY_TRIGGER = 8      # only when substring results are genuinely sparse
FUZZY_MIN_LEN = 3
FUZZY_MAX_LEN = 24
FUZZY_MAX_HITS = 2000
FUZZY_CEILING = 140.0

# What a vendored or bundled path costs. It has to outweigh a strong name
# match, because a strong name match is exactly what it is up against:
# "delta" is an exact stem match for delta.pyi inside a bundled stub package,
# worth 980, while the boarding pass actually wanted only contains the word,
# worth 420. At 260 the stub won and the document sat at rank 9. At 550 the
# document is rank 3, and the ranking evaluation is unmoved at 13/13 anywhere
# between 260 and 700.
NOISE_PENALTY = 550.0

HOME_BONUS = 70.0
# Recently edited files are usually the ones you mean. Kept modest so it breaks
# ties and lifts "the updated one" without overriding how well a name matched:
# 158 resumes differing only by folder are separated by date, not by name.
RECENCY_BONUS = 90.0
RECENCY_HALF_LIFE_DAYS = 45.0
# How many rows one filename may claim before the rest queue behind whatever
# else matched. See Searcher._spread for why this defers instead of penalising.
DUPLICATE_QUOTA = 2
# Paths that are real but rarely what was meant. Milder than NOISE, because
# these are legitimate hits -- just not the canonical one.
DEMOTED = (
    # The 32-bit mirror of System32: same filenames, almost never the copy you
    # want on a 64-bit machine.
    ("\\syswow64\\", 60.0),
    # Shell bookkeeping: shortcuts Windows generated, not ones you made. They
    # open the right thing, but the folder itself is the better answer.
    ("\\links\\", 120.0),
    ("\\windows\\recent\\", 120.0),
    ("\\microsoft\\windows\\start menu\\programs\\startup\\", 60.0),
)

# The Start Menu is Windows' own list of installed programs, so an entry there
# is the canonical way to launch one. Searching for a media player returned its
# folder, its logo, its installer and its playlist above the program itself.
START_MENU = "\\start menu\\programs\\"
START_MENU_BONUS = 80.0
# An installer is not the program: it is what you ran once to get it, and it
# usually sits in Downloads, where it collects the bonus for being your own
# file. Applied only when the query did not ask for one.
INSTALLER_PENALTY = 90.0

NUL = b"\x00"

BOUNDARY_BYTES = frozenset(b" _-.()[]{}#@,+~\\/'")

# What carrying an extension predicts about whether you meant this file.
#
# Video sits at the same weight as a document because it earns it: of the 1,047
# videos on the development machine 93% are in Downloads, Videos or Desktop and
# 2% are bundled inside an application -- almost exactly the split for PDFs, at
# 94% and 3%. Having no entry at all meant a video scored zero for being a
# video and lost to every PDF on the machine.
#
# Photos and audio are deliberately left where they are, because the same
# measurement says they are not the same kind of thing. Only 15% of the 22,240
# images are the user's own; the rest are icons and application resources, and
# raising the extension would raise all of them. Audio is 6%, being mostly
# effects shipped inside games. What distinguishes a photo you took from an
# icon is where it lives, and the home folder bonus already carries that.
EXT_BONUS = {
    b"exe": 90, b"lnk": 90, b"bat": 70, b"cmd": 70, b"ps1": 70, b"msi": 50,
    b"pdf": 30, b"docx": 30, b"xlsx": 30, b"pptx": 30,
    b"mp4": 30, b"mkv": 30, b"mov": 30, b"avi": 30, b"webm": 30, b"m4v": 30,
    b"md": 25, b"txt": 25, b"py": 25, b"js": 25, b"ts": 25,
    b"json": 15, b"png": 15, b"jpg": 15,
}

NOISE = (
    "\\appdata\\local\\temp\\", "\\appdata\\local\\packages\\",
    "\\node_modules\\", "\\.git\\", "\\__pycache__\\", "\\windows\\winsxs\\",
    "\\$recycle.bin\\", "\\site-packages\\", "\\.cache\\",
    # Bundled application internals. An Electron app ships thousands of icons
    # and stubs with ordinary words for names: VS Code's media-player
    # "resume.svg" was outranking every real resume document on the machine.
    "\\resources\\app\\", "\\user data\\default\\extensions\\",
    # Somebody else's source, shipped inside something you installed. Measured
    # across nineteen everyday queries these were 28% of the top ten results,
    # and typing "delta" returned eight of them against one real file. They
    # are neither system files nor yours, which is why looking only at
    # C:\Windows found almost nothing wrong: that was 10%, and most of it was
    # cmd.exe and notepad.exe, which are the point rather than the problem.
    "\\vendor_perl\\", "\\typeshed-fallback\\", "\\bundled\\stubs\\",
    "\\.vscode\\extensions\\", "\\share\\doc\\", "\\vendor\\",
)


def _words(name: str) -> list:
    """The words of a name, lowercased, without its extension.

    Word breaks are the separators people type around plus the humps in
    CamelCase, so "VisualStudioCode" reads the same as the spaced form.
    """
    stem = name.rsplit(".", 1)[0] if "." in name[1:] else name
    words = []
    current = ""
    previous = ""
    for character in stem:
        if not character.isalnum():
            if current:
                words.append(current.lower())
            current = ""
        elif character.isupper() and previous.islower():
            if current:
                words.append(current.lower())
            current = character
        else:
            current += character
        previous = character
    if current:
        words.append(current.lower())
    return words


def _initials(name: str) -> str:
    """First letters of the words in a name: "Visual Studio Code" -> "vsc"."""
    return "".join(word[0] for word in _words(name))


def _squashed(name: str) -> str:
    """The name with its separators taken out, for matching across them.

    People do not put the spaces where the file does. A boarding pass called
    "Find Your Trip_ Delta Air Lines.pdf" is not found by "delta airlines",
    because the file says "Air Lines" and the query says "airlines" -- neither
    is a substring of the other. Squashed, one is.
    """
    return "".join(character for character in name.lower()
                   if character.isalnum())


def _alias_forms(name: str):
    """The ways somebody might write a program's name as one word.

    The first k words shorten to their initials and the rest stay whole, for
    every k, which is the shape these actually take: "Visual Studio Code"
    gives visualstudiocode, vstudiocode, vscode and vsc. The obvious rule --
    the initial of every word -- only ever produces "vsc", and people type
    "vscode".

    Not every abbreviation is reachable this way and that is fine. "msword"
    would need the first two letters of Microsoft rather than the first one,
    and guessing at prefixes as well would match far more than it should.
    """
    words = _words(name)
    if not words or len(words) > ALIAS_MAX_WORDS:
        return ()
    forms = set()
    for split in range(len(words) + 1):
        form = "".join(word[0] for word in words[:split])
        form += "".join(words[split:])
        if len(form) >= ALIAS_MIN_LEN:
            forms.add(form)
    return forms


class Result:
    __slots__ = ("index", "name", "path", "is_dir", "score", "fuzzy")

    def __init__(self, index, name, path, is_dir, score, fuzzy=False):
        self.index = index
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.score = score
        self.fuzzy = fuzzy

    def __repr__(self):
        return f"Result({self.path!r}, score={self.score:.1f})"


class Searcher:
    def __init__(self, index, usage=None):
        self.index = index
        self.usage = usage
        home = os.path.expanduser("~")
        self._home = (home.lower() + "\\") if os.path.isdir(home) else ""
        self._hay = b""
        self._offsets = array.array("q")
        self._aliases: dict[str, list[int]] = {}
        self._last_primary = ""
        self._last_hits: list[int] | None = None
        self._now = time.time()
        # Name matches for the most recent single-word query, so the UI can say
        # "40 of 535" instead of implying 40 is all there is. None when a
        # multi-word query makes the figure meaningless.
        self.last_total = None
        self.last_total_capped = False
        self.build()

    def build(self) -> None:
        """Assemble the flat haystack. Called once per index load."""
        idx = self.index
        buf = bytearray()
        offsets = array.array("q")
        name = idx.name
        aliases = {}
        for i in range(len(idx)):
            raw = name(i)
            lowered = raw.lower()
            buf += NUL
            offsets.append(len(buf))
            buf += lowered.encode("utf-8")
            if lowered.endswith(ALIAS_EXTENSIONS):
                for form in _alias_forms(raw):
                    aliases.setdefault(form, []).append(i)
        buf += NUL
        offsets.append(len(buf))
        self._hay = bytes(buf)
        self._offsets = offsets
        self._aliases = aliases
        self._last_primary = ""
        self._last_hits = None

    def _low(self, i: int) -> bytes:
        """The lowercase name of entry i, as a slice of the haystack."""
        return self._hay[self._offsets[i]:self._offsets[i + 1] - 1]

    def _name_at(self, start: int) -> bytes:
        """The lowercase name beginning at ``start``."""
        return self._hay[start:self._hay.find(NUL, start)]

    def _index_at(self, start: int) -> int:
        """Entry index for a name start. Deliberately called sparingly."""
        return bisect_right(self._offsets, start) - 1

    # -- scanning ----------------------------------------------------------

    @staticmethod
    def _scan_limit(token_length: int) -> int:
        """A vague query cannot rank usefully, so it does not earn a deep scan."""
        if token_length <= 2:
            tier = 4000
        elif token_length == 3:
            tier = 8000
        else:
            tier = MAX_SCAN_HITS
        return min(tier, MAX_SCAN_HITS)

    def _scan(self, needle: bytes, limit: int):
        """Start offsets of names containing ``needle``, at most ``limit``."""
        hay = self._hay
        hits: list[int] = []
        find = hay.find
        rfind = hay.rfind
        needle_len = len(needle)
        pos = find(needle)
        capped = False
        while pos != -1:
            # A name starts just after the NUL preceding the match.
            hits.append(rfind(NUL, 0, pos) + 1)
            if len(hits) >= limit:
                capped = True
                break
            # Jump to the NUL closing this name so an entry can only ever be
            # reported once, however many times the needle occurs inside it.
            end = find(NUL, pos + needle_len)
            if end == -1:
                break
            pos = find(needle, end)
        return hits, capped

    @staticmethod
    def _subsequence_pattern(primary: str) -> bytes:
        """Build a backtracking-free subsequence pattern.

        The obvious form, ``a[^NUL]*?b[^NUL]*?c``, backtracks catastrophically:
        an 11-character query took 1.7 seconds over a 15MB haystack. Excluding
        the *next* wanted character from each gap makes every step
        deterministic -- the gap can only end at the first occurrence of that
        character, which is exactly the greedy leftmost subsequence match.
        """
        chars = [ch.encode("utf-8") for ch in primary]
        parts = [re.escape(chars[0])]
        for char in chars[1:]:
            if len(char) == 1:
                escaped = char if char not in b"\\]^-" else b"\\" + char
                gap = b"[^" + NUL + escaped + b"]*"
            else:
                # A byte class cannot express a multi-byte character; fall back.
                gap = b"[^" + NUL + b"]*?"
            parts.append(gap + re.escape(char))
        return b"".join(parts)

    def _fuzzy_scan(self, primary: str, exclude: set[int]):
        """Subsequence fallback: 'rpt' reaches 'report.txt'.

        One regex over the whole haystack, bounded so a match can never cross a
        NUL into the next name.
        """
        try:
            compiled = re.compile(self._subsequence_pattern(primary))
        except re.error:
            return []

        hay = self._hay
        rfind = hay.rfind
        found = []
        for match in compiled.finditer(hay):
            begin = match.start()
            start = rfind(NUL, 0, begin) + 1
            if start in exclude:
                continue
            exclude.add(start)
            span = match.end() - begin
            # A tight match is a better match; starting the name is better still.
            score = FUZZY_CEILING - (span - len(primary)) * 2.0
            if begin == start:
                score += 30.0
            found.append((start, max(10.0, score)))
            if len(found) >= FUZZY_MAX_HITS:
                break
        return found

    # -- ranking -----------------------------------------------------------

    def _base_score(self, start: int, query: bytes, primary: bytes,
                    stem_exact: bool = False) -> float:
        """Name-only score, read in place off the haystack.

        Indexes into the shared buffer rather than slicing a name out of it:
        this runs once per candidate, up to thousands of times per keystroke,
        and the slice was the allocation that dominated.
        """
        hay = self._hay
        end = hay.find(NUL, start)
        length = end - start
        dot = hay.rfind(b".", start, end)
        stem = (dot - start) if dot > start else length

        if length == len(query) and hay.startswith(query, start):
            score = 1000.0
        elif stem_exact and stem == len(primary) and hay.startswith(primary, start):
            # Typing "cmd" means cmd.exe, not a vendor folder named cmd. An
            # extensionless query matching a whole stem is an exact hit, and
            # must be tested before the generic prefix case or "cmd.exe" stops
            # at 720 for merely starting with "cmd".
            score = 980.0
        elif hay.startswith(query, start):
            score = 720.0
        elif hay.startswith(primary, start):
            score = 640.0
        else:
            at = hay.find(primary, start, end)
            score = 420.0 if at > start and hay[at - 1] in BOUNDARY_BYTES else 160.0

        if dot > start:
            score += EXT_BONUS.get(hay[dot + 1:end], 0)
        score -= min(length, 80) * 0.6
        return score

    def _alias_score(self, start: int) -> float:
        """Score for a name somebody typed as a program rather than a file.

        Carries the same kind and length terms a name match does, because what
        the file *is* still matters. Without the extension term a policy
        template called VSCode.admx outscored Visual Studio Code itself: the
        template is an exact stem match for "vscode", which is the strongest
        name tier there is, and an alias that ignored being a shortcut had
        nothing to answer that with.
        """
        hay = self._hay
        end = hay.find(NUL, start)
        length = end - start
        dot = hay.rfind(b".", start, end)
        score = ALIAS_SCORE
        if dot > start:
            score += EXT_BONUS.get(hay[dot + 1:end], 0)
        score -= min(length, 80) * 0.6
        return score

    def _adjust(self, i, score, lowered_path, rest) -> float:
        """Path-aware corrections, applied only to entries that reach the pool."""
        if self.index.isdir[i]:
            score += 15.0
        score -= self.index.depth(i) * 2.0
        # Your own files beat identically named ones in another profile, a
        # system folder, or a vendored copy. AppData is excluded -- technically
        # home, but nothing in it was put there by you -- except
        # AppData\Local\Programs, which is where per-user app installs (Python,
        # VS Code) actually live and which you did choose to put there.
        if self._home and lowered_path.startswith(self._home):
            if ("\\appdata\\" not in lowered_path
                    or "\\appdata\\local\\programs\\" in lowered_path):
                score += HOME_BONUS
        noisy = False
        for marker in NOISE:
            if marker in lowered_path:
                score -= NOISE_PENALTY
                noisy = True
                break
        for marker, penalty in DEMOTED:
            if marker in lowered_path:
                score -= penalty
        lowered_name = self.index.name(i).lower()
        if START_MENU in lowered_path and "uninstall" not in lowered_name:
            score += START_MENU_BONUS
        if not self._wants_installer and kinds.kind_of(
                lowered_name, bool(self.index.isdir[i])) == kinds.INSTALLER:
            score -= INSTALLER_PENALTY
        # What you have actually opened, bounded so it reorders comparable
        # matches rather than promoting something irrelevant.
        if self.usage is not None:
            score += self.usage.boost(lowered_path)
        # Recency means "you were working on this". Inside a package directory
        # it means nothing of the sort: updating an editor restamps thousands
        # of bundled files at once, which made its icons look freshly edited.
        if not noisy:
            mtime = self.index.mtimes[i] if i < len(self.index.mtimes) else 0
            if mtime:
                age_days = (self._now - mtime) / 86400.0
                if age_days < 0:
                    age_days = 0.0
                score += RECENCY_BONUS * (0.5 ** (age_days
                                                  / RECENCY_HALF_LIFE_DAYS))
        if rest:
            lowered_name = self.index.name(i).lower()
            for token in rest:
                # Reward tokens hitting the name rather than some ancestor folder.
                if token in lowered_name:
                    score += 40.0
        return score

    # -- public ------------------------------------------------------------

    def search(self, query: str, limit: int = 60, fuzzy: bool = True):
        query = query.strip().lower()
        if not query:
            self._last_primary = ""
            self._last_hits = None
            self.last_total = None
            self.last_total_capped = False
            return []

        self._now = time.time()
        # Ask for an installer and you get installers; otherwise they are the
        # thing standing between you and the program you meant.
        self._wants_installer = any(word in query
                                    for word in kinds.INSTALLER_WORDS)
        # Resolving a path is what a result costs, so only build as many as
        # were asked for. The floor keeps the interleaving in `_spread` a fair
        # fight even when the caller wants a handful.
        pool = min(REFINE_POOL, max(limit, SPREAD_POOL))
        tokens = query.split()
        primary_text, rest = tokens[0], tokens[1:]
        primary = primary_text.encode("utf-8")
        query_bytes = query.encode("utf-8")
        # Candidate lists from any word other than the first, each scored
        # against the word that actually appears in the name.
        others = []

        # The cache is keyed on the first token alone, so adding or extending a
        # later token reuses it without touching the haystack at all.
        reusable = (
            self._last_hits is not None
            and self._last_primary
            and primary_text.startswith(self._last_primary)
        )
        if reusable:
            hay = self._hay
            find = hay.find
            candidates = [start for start in self._last_hits
                          if primary in hay[start:find(NUL, start)]]
            capped = False
        else:
            candidates, capped = self._scan(primary, self._scan_limit(len(primary)))

        # The first word is not always the one written in the name.
        #
        # A capped scan is the first few thousand names containing the word,
        # in index order -- an arbitrary slice rather than the best matches --
        # so the words that would actually identify the file only got to
        # filter that slice. Typing the exact name of "A Night in Mondstadt"
        # found nothing, because "a" filled the cap long before reaching it.
        #
        # And a word of two or three letters is as likely to be an initialism
        # as a substring: nothing in "Visual Studio Code" contains "vs" to
        # find, so no amount of filtering reaches it.
        #
        # Either way the longest word is scanned as well and the two candidate
        # lists are merged. Scanning it *instead* was tried and is wrong: it
        # fixed "vs code" by breaking "ui quickfind", where the short word is
        # the filename and the long one the folder.
        #
        # Outside the cache branch above, deliberately. Having this run only
        # when the cache missed meant it never ran at all in the application,
        # where a query is typed a character at a time and every keystroke
        # hits the cache -- "vs code" worked when issued cold and did nothing
        # when typed, which is the only way anybody types it.
        if len(tokens) > 1:
            initialish = any(len(token) <= INITIALISM_MAX for token in tokens)
            pick = max(range(len(tokens)), key=lambda n: len(tokens[n]))
            if (capped or initialish) and tokens[pick] != primary_text:
                alternate = tokens[pick]
                extra, extra_capped = self._scan(
                    alternate.encode("utf-8"),
                    self._scan_limit(len(alternate)))
                others.append((alternate, extra))
                capped = capped and extra_capped

        # A capped scan is an incomplete set; caching it would leak the gap into
        # every query that extends this one.
        if capped:
            self._last_primary = ""
            self._last_hits = None
        else:
            self._last_primary = primary_text
            self._last_hits = candidates

        # A query with no dot and no space is a name, not a filename: matching
        # a whole stem then counts as exact.
        stem_exact = len(tokens) == 1 and b"." not in primary
        base = self._base_score
        # Only meaningful for a single word: with further words the count of
        # name matches is not the count of results.
        self.last_total = len(candidates) if not rest else None
        self.last_total_capped = capped

        scored = [(base(start, query_bytes, primary, stem_exact), start)
                  for start in candidates]
        already = set(candidates)

        # What people call a program, written as one word. Outside the cache
        # branch deliberately: a query is typed a character at a time, so the
        # cache hits on every keystroke, and anything done only on a miss is
        # done only when a query arrives cold -- which is not how anyone types.
        alias_starts = set()
        for i in self._aliases.get("".join(tokens), ()):
            start = self._offsets[i]
            if start in already:
                continue
            already.add(start)
            alias_starts.add(start)
            scored.append((self._alias_score(start), start))

        if others:
            for word, hits in others:
                word_bytes = word.encode("utf-8")
                for start in hits:
                    if start in already:
                        continue
                    already.add(start)
                    # Scored against its own word: judging "Code.exe" by how
                    # well it matches "vs" would put it under everything.
                    scored.append(
                        (base(start, query_bytes, word_bytes, False), start))

        fuzzy_scores = {}
        if (fuzzy and len(scored) < FUZZY_TRIGGER
                and FUZZY_MIN_LEN <= len(primary_text) <= FUZZY_MAX_LEN):
            # Everything already found, not just the substring hits: an
            # entry added by an alias or a second word is also a subsequence
            # match, and adding it twice let the fuzzy ceiling pull the good
            # copy down to the guess's score.
            for start, score in self._fuzzy_scan(primary_text, already):
                fuzzy_scores[start] = score
                scored.append((score, start))

        if not scored:
            return []

        scored.sort(key=lambda pair: -pair[0])

        results = []
        seen = set()
        probes = 0
        resolve = self.index.path
        locate = self._index_at
        name_of = self.index.name
        isdir = self.index.isdir
        for score, start in scored:
            probes += 1
            if probes > MAX_PATH_PROBES:
                break
            # Only now is the entry index needed, so only now is it looked up.
            i = locate(start)
            full_path = resolve(i)
            lowered_path = full_path.lower()
            abbreviated = False
            if rest:
                # Every word, not just the ones after the first: a candidate
                # found through the second scan has that word in its name and
                # the first one still to account for.
                skip = False
                initials = None
                squashed = None
                for token in tokens:
                    if token in lowered_path:
                        continue
                    # Not written anywhere in the path, but people shorten the
                    # names of the things they use: "vs code" for Visual
                    # Studio Code, "ms word" for Microsoft Word. Nothing in
                    # "Visual Studio Code.lnk" contains "vs", so the file was
                    # not merely ranked low, it was filtered out.
                    if initials is None:
                        initials = _initials(name_of(i))
                    if initials.startswith(token):
                        abbreviated = True
                        continue
                    # Written in the name, just with a separator through it:
                    # "airlines" against "Delta Air Lines".
                    if squashed is None:
                        squashed = _squashed(name_of(i))
                    if token in squashed:
                        continue
                    skip = True
                    break
                if skip:
                    continue
            # The supplement walk overlaps what MFT already found, so the same
            # file can appear twice; collapse on the path the user sees.
            if lowered_path in seen:
                continue
            seen.add(lowered_path)
            final = self._adjust(i, score, lowered_path, rest)
            if abbreviated and start not in alias_starts:
                # Guessed at from initials rather than read off the name, so
                # it sits under everything spelled out: exact name, then
                # prefix, then word boundary, then this, then the subsequence
                # fallback. Being found at all is the point.
                #
                # An alias is exempt: "vs code" reaches Visual Studio Code by
                # initials too, but it got there by being the name of a
                # program, which is a better reason.
                final = min(final, ABBREV_CEILING)
            is_fuzzy = start in fuzzy_scores
            if is_fuzzy:
                # _adjust can add a per-token bonus; clamp so a subsequence hit
                # can never climb above a genuine substring hit.
                final = min(final, FUZZY_CEILING)
            results.append(Result(i, name_of(i), full_path,
                                  bool(isdir[i]), final, fuzzy=is_fuzzy))
            if len(results) >= pool:
                break

        results.sort(key=lambda r: -r.score)
        return self._spread(results)[:limit]

    @staticmethod
    def _spread(results):
        """Interleave so one filename cannot monopolise the visible rows.

        This defers rather than penalises. Scoring duplicates down was the
        obvious approach and it failed both ways: a steep penalty buried 158
        resumes that differed only by folder, and a shallow one let eight
        vendored clones push the single distinctly named file off the end.

        Deferring has no such tension. Every name keeps its score and its best
        copy stays ahead of its worse ones. Copies past the quota simply queue
        behind whatever variety exists, so they are still reachable by
        scrolling instead of being pushed below unrelated results.
        """
        seen = {}
        front, deferred = [], []
        for result in results:
            key = result.name.lower()
            count = seen.get(key, 0)
            seen[key] = count + 1
            (front if count < DUPLICATE_QUOTA else deferred).append(result)
        return front + deferred
