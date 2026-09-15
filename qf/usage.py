"""What you have opened before, and how much that should count.

Frecency: frequency and recency together. A file you opened twice this morning
should outrank forty files with the same name that you have never touched, but
it should not outrank a genuinely better match for a different query -- so the
boost is bounded and decays.

The store is a small JSON file on this machine. It records paths you opened
through QuickFind and nothing else; `frecency: false` disables it and
``:forget`` erases it.
"""
from __future__ import annotations

import json
import math
import os
import time

# One open is worth about as much as a word-boundary match; a well-worn file
# reaches the cap. Deliberately below the gap between match classes, so history
# reorders comparable matches rather than promoting irrelevant ones.
BASE_BOOST = 110.0
MAX_BOOST = 300.0
GROWTH = 0.6
HALF_LIFE_DAYS = 30.0

MAX_ENTRIES = 2000
OPEN_WEIGHT = 1.0
# Revealing counts the same as opening. Both mean "this is the file I was
# looking for"; the only difference is what you do with it next. Weighting
# reveal lower punished the perfectly normal habit of navigating to a file
# rather than launching it.
REVEAL_WEIGHT = 1.0


def default_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "quickfind")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "usage.json")


class UsageStore:
    """Path -> (weight, last opened). Small, local, and disposable."""

    def __init__(self, path: str | None = None, max_entries: int = MAX_ENTRIES,
                 enabled: bool = True):
        self.path = path or default_path()
        self.max_entries = max_entries
        self.enabled = enabled
        self._entries: dict[str, list[float]] = {}
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                stored = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(stored, dict):
            return
        entries = stored.get("entries", {})
        if not isinstance(entries, dict):
            # The file is guarded above, but not what is inside it: a truncated
            # or hand-edited store with `entries` as anything else raised here,
            # and this runs while the Controller is being built, so it took the
            # whole launcher down rather than costing a little history.
            return
        for key, value in entries.items():
            try:
                weight, last = float(value[0]), float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            self._entries[key] = [weight, last]

    def save(self) -> None:
        payload = {"version": 1, "entries": self._entries}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- recording ---------------------------------------------------------

    def record(self, path: str, weight: float = OPEN_WEIGHT) -> None:
        if not self.enabled or not path:
            return
        key = path.lower()
        entry = self._entries.get(key)
        now = time.time()
        if entry is None:
            self._entries[key] = [weight, now]
        else:
            entry[0] += weight
            entry[1] = now
        if len(self._entries) > self.max_entries:
            self._prune()
        self.save()

    def _prune(self) -> None:
        """Drop the entries worth the least right now, not merely the oldest."""
        ranked = sorted(self._entries.items(),
                        key=lambda item: self._value(item[1]), reverse=True)
        keep = ranked[:self.max_entries * 3 // 4]
        self._entries = {key: value for key, value in keep}

    def clear(self) -> None:
        self._entries = {}
        try:
            os.remove(self.path)
        except OSError:
            pass

    # -- scoring -----------------------------------------------------------

    def _value(self, entry) -> float:
        weight, last = entry
        if weight <= 0:
            return 0.0
        age_days = max(0.0, (time.time() - last) / 86400.0)
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        # Scales below 1.0 as well as above it, so a reveal -- a weaker signal
        # than an open -- actually scores lower instead of being floored to the
        # same value.
        strength = max(0.1, 1.0 + GROWTH * math.log2(weight))
        return min(MAX_BOOST, BASE_BOOST * strength * recency)

    def boost(self, lowered_path: str) -> float:
        """Extra score for a path you have opened. 0 when unknown."""
        if not self.enabled or not self._entries:
            return 0.0
        entry = self._entries.get(lowered_path)
        return self._value(entry) if entry else 0.0

    def __len__(self) -> int:
        return len(self._entries)
