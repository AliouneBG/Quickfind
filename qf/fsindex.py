"""Filesystem index: NTFS MFT enumeration with a portable scandir fallback.

Names are stored as one packed UTF-8 blob plus an offsets array, not as a list
of str. Two reasons, both measured on a 754k-entry index:

* A list of 754k short strings costs ~52MB, almost all of it per-object
  overhead (~60 bytes each for ~20 bytes of text).
* A Python str uses a uniform width for every character, so a single filename
  containing an emoji promotes an entire multi-megabyte string to 4 bytes per
  character. One such file exists on a typical drive and quadrupled the
  haystack. bytes has no such cliff.

Paths are reconstructed by walking the parent chain rather than stored.
"""
from __future__ import annotations

import array
import ctypes
import os
import pickle
import stat as stat_module
import struct
import time
from ctypes import wintypes

FILE_ATTRIBUTE_DIRECTORY = 0x10
FSCTL_ENUM_USN_DATA = 0x000900B3
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_HANDLE_EOF = 38

CACHE_VERSION = 5
PATH_CACHE_MAX = 60_000
DEFAULT_EXCLUDES = ("$recycle.bin", "system volume information", "winsxs")

# MFT enumeration returns one record per file, so a file with several hardlink
# names is reported under only one of them. Windows system binaries are
# hardlinks into WinSxS, which left 89% of System32 -- cmd.exe, calc.exe,
# control.exe -- findable only under their WinSxS path. Walking these
# directories afterwards restores the names people actually search for.
DEFAULT_SUPPLEMENT = (r"C:\Windows",)

# USN_RECORD_V2: length/versions/FRN/parent FRN at 0, then attributes and the
# name's length and offset at 52. Precompiled because this runs 1.4M times.
_USN_HEAD = struct.Struct("<IHHQQqq")
_USN_TAIL = struct.Struct("<IHH")
_MFT_BUFFER = 1 << 22


class _NameView:
    """Sequence view over the packed blob, so ``index.names[i]`` still works."""

    __slots__ = ("_index",)

    def __init__(self, index):
        self._index = index

    def __len__(self):
        return len(self._index)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self._index.name(k) for k in range(*i.indices(len(self)))]
        if i < 0:
            i += len(self)
        return self._index.name(i)

    def __iter__(self):
        name = self._index.name
        for i in range(len(self)):
            yield name(i)

    def __contains__(self, value):
        return any(value == existing for existing in self)

    def __eq__(self, other):
        return list(self) == list(other)

    def __repr__(self):
        return f"<names n={len(self)}>"


class Index:
    """Flat parallel-array store of every indexed filesystem entry."""

    __slots__ = ("blob", "starts", "parents", "isdir", "mtimes", "roots",
                 "built_at", "source", "_depth", "_path_cache")

    def __init__(self) -> None:
        self.blob = bytearray()
        self.starts = array.array("q", [0])
        self.parents = array.array("i")
        self.isdir = bytearray()
        # Seconds since the epoch, 0 when unknown. uint32 is good until 2106
        # and costs 4 bytes an entry instead of 8.
        self.mtimes = array.array("I")
        self.roots: dict[int, str] = {}
        self.built_at = 0.0
        self.source = "empty"
        self._depth = None
        self._path_cache: dict[int, str] = {}

    def __len__(self) -> int:
        return len(self.parents)

    @property
    def names(self):
        return _NameView(self)

    def add(self, name: str, parent: int, isdir: bool, mtime: int = 0) -> int:
        self.blob += name.encode("utf-8")
        self.starts.append(len(self.blob))
        self.parents.append(parent)
        self.isdir.append(1 if isdir else 0)
        self.mtimes.append(mtime if 0 < mtime < 0xFFFFFFFF else 0)
        return len(self.parents) - 1

    def name(self, i: int) -> str:
        return self.blob[self.starts[i]:self.starts[i + 1]].decode("utf-8", "replace")

    def raw_name(self, i: int) -> bytes:
        return bytes(self.blob[self.starts[i]:self.starts[i + 1]])

    def path(self, i: int) -> str:
        """Reconstruct a full path, memoising every directory on the way.

        Results overwhelmingly share ancestors, so caching directory prefixes
        turns a per-result walk that re-decoded every parent into one decode
        plus a concat. Filtering 6000 candidates by a second token dropped from
        ~106ms to a few ms on this alone.
        """
        cache = self._path_cache
        pending = []
        node = i
        guard = 0
        while node >= 0 and guard < 256:
            if node in cache:
                break
            pending.append(node)
            node = self.parents[node]
            guard += 1

        current = cache.get(node, "") if node >= 0 else ""
        isdir = self.isdir
        name = self.name
        for node in reversed(pending):
            part = name(node)
            current = part if not current else current + "\\" + part
            if isdir[node]:
                if len(cache) >= PATH_CACHE_MAX:
                    cache.clear()
                cache[node] = current
        return current

    def parent_path(self, i: int) -> str:
        p = self.parents[i]
        return self.path(p) if p >= 0 else ""

    def depth(self, i: int) -> int:
        if self._depth is None:
            self._build_depth()
        return self._depth[i]

    def _build_depth(self) -> None:
        n = len(self)
        d = array.array("i", bytes(4 * n))
        parents = self.parents
        for i in range(n):
            p = parents[i]
            if p < 0:
                d[i] = 0
            elif p < i:
                d[i] = d[p] + 1
            else:
                # MFT order can place a parent after its child; resolve by chain.
                depth = 0
                j = i
                guard = 0
                while j >= 0 and guard < 256:
                    j = parents[j]
                    depth += 1
                    guard += 1
                d[i] = depth - 1
        self._depth = d


# ---------------------------------------------------------------------------
# scandir walker (no privileges required)
# ---------------------------------------------------------------------------

def build_by_walk(roots, excludes=DEFAULT_EXCLUDES, progress=None) -> Index:
    idx = Index()
    idx.source = "walk"
    skip = {e.lower() for e in excludes}

    for root in roots:
        label = root.rstrip("\\/") or root
        if not os.path.isdir(label + os.sep):
            continue
        ri = idx.add(label, -1, True)
        idx.roots[ri] = label
        stack = [(ri, label + os.sep)]

        while stack:
            parent_i, parent_path = stack.pop()
            try:
                scanner = os.scandir(parent_path)
            except OSError:
                continue
            with scanner:
                while True:
                    try:
                        entry = next(scanner)
                    except StopIteration:
                        break
                    except OSError:
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    try:
                        # scandir carries stat data on Windows, so this costs
                        # no extra syscall.
                        mtime = int(entry.stat(follow_symlinks=False).st_mtime)
                    except (OSError, ValueError, OverflowError):
                        mtime = 0
                    i = idx.add(entry.name, parent_i, is_dir, mtime)
                    if not is_dir or entry.name.lower() in skip:
                        continue
                    try:
                        if entry.is_symlink() or entry.is_junction():
                            continue
                    except OSError:
                        continue
                    stack.append((i, os.path.join(parent_path, entry.name)))

            if progress is not None:
                progress(len(idx))

    idx.built_at = time.time()
    return idx


# ---------------------------------------------------------------------------
# NTFS MFT enumeration (requires Administrator)
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def open_volume(letter: str):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    volume = "\\\\.\\" + letter + ":"
    handle = kernel32.CreateFileW(
        volume, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(),
                      "cannot open " + volume + " (needs admin)")
    return kernel32, handle


def build_by_mft(drive: str) -> Index:
    """Enumerate every in-use record in a volume's MFT."""
    letter = drive.rstrip("\\/:")[:1].upper()
    kernel32, handle = open_volume(letter)

    idx = Index()
    idx.source = "mft"
    label = letter + ":"
    root_i = idx.add(label, -1, True)
    idx.roots[root_i] = label

    frns: list[int] = []
    parent_frns: list[int] = []
    append_frn = frns.append
    append_parent = parent_frns.append
    blob = idx.blob
    starts = idx.starts
    parents = idx.parents
    isdir = idx.isdir
    mtimes = idx.mtimes
    head_unpack = _USN_HEAD.unpack_from
    tail_unpack = _USN_TAIL.unpack_from

    buf = ctypes.create_string_buffer(_MFT_BUFFER)
    view = memoryview(buf)
    returned = wintypes.DWORD(0)
    start_frn = 0

    try:
        while True:
            med = struct.pack("<Qqq", start_frn, 0, (1 << 63) - 1)
            ok = kernel32.DeviceIoControl(
                handle, FSCTL_ENUM_USN_DATA,
                med, len(med), buf, _MFT_BUFFER,
                ctypes.byref(returned), None,
            )
            if not ok:
                err = ctypes.get_last_error()
                if err in (ERROR_HANDLE_EOF, 0):
                    break
                raise OSError(err, f"FSCTL_ENUM_USN_DATA failed on {label}")
            n = returned.value
            if n <= 8:
                break
            # A memoryview avoids copying the whole buffer once per call, and
            # str(view, ...) decodes each name without an intermediate bytes.
            chunk = view[:n]
            start_frn = struct.unpack_from("<Q", chunk, 0)[0]
            offset = 8
            while offset < n:
                (rec_len, _major, _minor, frn, pfrn, _usn,
                 filetime) = head_unpack(chunk, offset)
                if rec_len == 0 or offset + rec_len > n:
                    break
                attrs, name_len, name_off = tail_unpack(chunk, offset + 52)
                name_at = offset + name_off
                name = str(chunk[name_at:name_at + name_len], "utf-16-le", "replace")

                # FILETIME counts 100ns ticks from 1601; shift to the epoch.
                stamp = filetime // 10_000_000 - 11_644_473_600
                blob += name.encode("utf-8")
                starts.append(len(blob))
                parents.append(-1)
                isdir.append(1 if attrs & FILE_ATTRIBUTE_DIRECTORY else 0)
                mtimes.append(stamp if 0 < stamp < 0xFFFFFFFF else 0)
                append_frn(frn)
                append_parent(pfrn)
                offset += rec_len
            chunk.release()
    finally:
        view.release()
        kernel32.CloseHandle(handle)

    frn_to_idx = {frn: k + 1 for k, frn in enumerate(frns)}
    parents = idx.parents
    for k, pfrn in enumerate(parent_frns):
        i = k + 1
        p = frn_to_idx.get(pfrn, root_i)
        parents[i] = root_i if p == i else p

    idx.built_at = time.time()
    return idx


# ---------------------------------------------------------------------------
# Orchestration + cache
# ---------------------------------------------------------------------------

def _is_drive_root(root: str) -> bool:
    stripped = root.rstrip("\\/")
    return len(stripped) == 2 and stripped[1] == ":"


def build(roots, excludes=DEFAULT_EXCLUDES, use_mft=True, progress=None,
          supplement=DEFAULT_SUPPLEMENT) -> Index:
    """Build an index, preferring MFT enumeration when every root is a drive."""
    if use_mft and is_admin() and roots and all(_is_drive_root(r) for r in roots):
        merged = Index()
        merged.source = "mft"
        try:
            for root in roots:
                _merge(merged, build_by_mft(root))
        except OSError:
            merged = None
        if merged is not None and len(merged):
            for folder in supplement or ():
                if os.path.isdir(folder):
                    _merge(merged, build_by_walk([folder], excludes))
            merged.source = "mft+walk" if supplement else "mft"
            merged.built_at = time.time()
            return merged
    return build_by_walk(roots, excludes, progress)


def from_paths(paths) -> Index:
    """Index a handful of explicit paths, adding ancestors as they are needed.

    The live folder watcher reports paths rather than a tree, and `path()`
    rebuilds a result by walking its parent chain, so every ancestor has to
    exist as an entry even though no-one searched for it.

    Each item is either a path, which is then stat'd, or a
    ``(path, is_dir, mtime)`` triple from something that has already looked.
    Stat'ing dominates the cost here, so the caller passing on what it knows
    takes a 3000 path rebuild from 178ms to about 10ms.
    """
    idx = Index()
    idx.source = "live"
    nodes: dict[str, int] = {}

    def ancestor(path: str) -> int:
        key = path.lower()
        existing = nodes.get(key)
        if existing is not None:
            return existing
        parent, name = os.path.split(path)
        if not name or parent == path:
            label = path.rstrip("\\/") or path
            i = idx.add(label, -1, True)
            idx.roots[i] = label
            nodes[key] = i
            return i
        i = idx.add(name, ancestor(parent), True)
        nodes[key] = i
        return i

    for item in paths:
        if isinstance(item, tuple):
            path, is_dir, mtime = item
        else:
            path = item
            try:
                info = os.stat(path)
                is_dir = stat_module.S_ISDIR(info.st_mode)
                mtime = int(info.st_mtime)
            except (OSError, ValueError, OverflowError):
                continue
        parent, name = os.path.split(path)
        if not name or not parent:
            continue
        key = path.lower()
        existing = nodes.get(key)
        if existing is not None:
            # Already present as somebody's ancestor, or repeated in the input.
            idx.mtimes[existing] = mtime if 0 < mtime < 0xFFFFFFFF else 0
            continue
        nodes[key] = idx.add(name, ancestor(parent), is_dir, mtime)

    idx.built_at = time.time()
    return idx


def _merge(target: Index, other: Index) -> None:
    shift = len(target)
    base = len(target.blob)
    target.blob += other.blob
    for k in range(1, len(other.starts)):
        target.starts.append(base + other.starts[k])
    target.isdir.extend(other.isdir)
    target.mtimes.extend(other.mtimes)
    for p in other.parents:
        target.parents.append(p if p < 0 else p + shift)
    for i, label in other.roots.items():
        target.roots[i + shift] = label


def cache_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "quickfind")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "index.pkl")


def save(idx: Index, path: str | None = None) -> str:
    path = path or cache_path()
    payload = {
        "version": CACHE_VERSION,
        "blob": bytes(idx.blob),
        "starts": idx.starts.tobytes(),
        "parents": idx.parents.tobytes(),
        "isdir": bytes(idx.isdir),
        "mtimes": idx.mtimes.tobytes(),
        "roots": idx.roots,
        "built_at": idx.built_at,
        "source": idx.source,
    }
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, path)
    return path


def load(path: str | None = None) -> Index | None:
    path = path or cache_path()
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
    except (OSError, pickle.PickleError, EOFError, AttributeError):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    idx = Index()
    idx.blob = bytearray(payload["blob"])
    idx.starts = array.array("q")
    idx.starts.frombytes(payload["starts"])
    idx.parents = array.array("i")
    idx.parents.frombytes(payload["parents"])
    idx.isdir = bytearray(payload["isdir"])
    idx.mtimes = array.array("I")
    idx.mtimes.frombytes(payload["mtimes"])
    idx.roots = payload["roots"]
    idx.built_at = payload["built_at"]
    idx.source = payload["source"]
    idx._path_cache = {}
    return idx
