"""Ranking evaluation: does the thing you meant come out on top?

The expectations below are judgements about intent, not facts -- someone typing
``cmd`` on a dev machine means the shell, not a vendor folder that happens to be
named cmd. They exist so ranking changes can be measured instead of argued
about. Entries whose target is missing on this machine are skipped rather than
counted as failures, so the set stays usable on other machines.
"""
from __future__ import annotations

import os

# Derived, not hardcoded: the set then works on any machine and carries no
# personal paths.
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")

# (query, expected path, why)
CASES = [
    ("cmd", r"C:\Windows\System32\cmd.exe",
     "typing a command name means the executable, not a folder called cmd"),
    ("notepad", r"C:\Windows\notepad.exe",
     "an app name means the app"),
    ("explorer", r"C:\Windows\explorer.exe",
     "an app name means the app"),
    ("regedit", r"C:\Windows\regedit.exe",
     "an app name means the app"),
    ("calc", r"C:\Windows\System32\calc.exe",
     "an app name means the app"),

    ("quickfind.py", os.path.join(PROJECT, "quickfind.py"),
     "an exact filename should be exact"),
    ("search.py", os.path.join(PROJECT, "qf", "search.py"),
     "your own file beats a vendored copy of the same name"),
    ("fsindex.py", os.path.join(PROJECT, "qf", "fsindex.py"),
     "unique name in your own project"),
    ("shellicon", os.path.join(PROJECT, "qf", "shellicon.py"),
     "stem without extension"),
    ("quickfind.vbs", os.path.join(PROJECT, "QuickFind.vbs"),
     "case-insensitive exact name"),

    ("downloads", os.path.join(HOME, "Downloads"),
     "your folder, not another user's or a nested copy"),
    ("desktop", os.path.join(HOME, "Desktop"),
     "your folder"),
    ("quickfind", PROJECT,
     "the project folder you work in"),
]


def resolve(cases=CASES):
    """Drop cases whose target does not exist on this machine."""
    return [(q, p, why) for q, p, why in cases if os.path.exists(p)]


def score(searcher, cases=None, limit=40):
    """Return per-case ranks plus top-1 / top-5 / MRR."""
    cases = resolve(cases or CASES)
    ranks = []
    for query, want, why in cases:
        searcher._last_hits = None
        searcher._last_primary = ""
        paths = [r.path.lower() for r in searcher.search(query, limit)]
        target = want.lower()
        rank = paths.index(target) + 1 if target in paths else None
        ranks.append((query, want, why, rank))

    found = [r for _, _, _, r in ranks if r]
    total = len(ranks)
    return {
        "cases": ranks,
        "total": total,
        "top1": sum(1 for r in found if r == 1),
        "top5": sum(1 for r in found if r <= 5),
        "found": len(found),
        "mrr": sum(1.0 / r for r in found) / total if total else 0.0,
    }


def crowding(searcher, queries, limit=10):
    """How many of the top rows repeat a filename already shown."""
    worst = []
    for query in queries:
        searcher._last_hits = None
        searcher._last_primary = ""
        names = [r.name.lower() for r in searcher.search(query, limit)]
        if names:
            worst.append((query, len(names) - len(set(names)), len(names)))
    return worst


def report(searcher, label=""):
    result = score(searcher)
    print(f"--- {label} ---")
    for query, want, _why, rank in result["cases"]:
        mark = "ok " if rank == 1 else ("   " if rank else "MISS")
        shown = rank if rank else "-"
        print(f"  {mark} {query!r:16} rank={shown!s:4} {want}")
    print(f"  top1 {result['top1']}/{result['total']}  "
          f"top5 {result['top5']}/{result['total']}  "
          f"found {result['found']}/{result['total']}  "
          f"MRR {result['mrr']:.3f}")
    return result
