"""Notice files created after the index was built, without administrator rights.

The NTFS change journal needs elevation, so an unprivileged session only ever
saw the filesystem as it was when the index was built: a file downloaded a
minute ago was unfindable until the next rebuild.

`ReadDirectoryChangesW` needs no privileges. It reports creations, renames and
deletions under a directory tree, and blocks until something happens, so an idle
machine costs nothing. It is used here only for the folders a person actually
saves into, because it reports paths rather than a volume-wide summary and the
churn under AppData is enormous.

When the kernel's buffer overflows it says only that changes were lost. That is
reported through `on_overflow` so the caller can fall back to a rebuild.
"""
from __future__ import annotations

import ctypes
import os
import stat
import struct
import threading
from ctypes import wintypes

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x0007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

FILE_NOTIFY_CHANGE_FILE_NAME = 0x0001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x0002
NOTIFY_FILTER = FILE_NOTIFY_CHANGE_FILE_NAME | FILE_NOTIFY_CHANGE_DIR_NAME

FILE_ACTION_ADDED = 1
FILE_ACTION_REMOVED = 2
FILE_ACTION_RENAMED_OLD_NAME = 4
FILE_ACTION_RENAMED_NEW_NAME = 5

BUFFER_SIZE = 1 << 16
# Folders no-one searches for by name, and which change constantly.
DEFAULT_EXCLUDES = ("appdata", "$recycle.bin", "node_modules", ".git",
                    "__pycache__", ".venv", "venv")
# A download in progress is renamed to its real name when it finishes, and the
# rename arrives as its own event, so the placeholder is not worth indexing.
PARTIAL_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download")


def default_folders():
    """Where files land: the user's own folders, if they exist."""
    home = os.path.expanduser("~")
    names = ("Downloads", "Desktop", "Documents", "Pictures", "Videos",
             "Music")
    found = [os.path.join(home, name) for name in names]
    return [path for path in found if os.path.isdir(path)]


def is_partial(name: str) -> bool:
    return name.lower().endswith(PARTIAL_SUFFIXES)


def _describe(path: str):
    """``(is_dir, mtime)``, or ``(False, None)`` if the file is already gone."""
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return False, None
    return stat.S_ISDIR(info.st_mode), int(info.st_mtime)


class FolderWatcher:
    """Calls ``on_change(path, removed, is_dir, mtime)`` as files come and go."""

    def __init__(self, folders, on_change, on_overflow=None,
                 excludes=DEFAULT_EXCLUDES):
        self.folders = [f for f in folders if f]
        self.on_change = on_change
        self.on_overflow = on_overflow
        self.excludes = {e.lower() for e in excludes}
        self.error = None
        self._threads = []
        self._handles = []
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if not self.folders:
            self.error = "no folders to watch"
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ctypes.c_void_p]
        kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        for folder in self.folders:
            handle = kernel32.CreateFileW(
                folder, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
            if not handle or handle == INVALID_HANDLE_VALUE:
                self.error = "cannot watch {}".format(folder)
                continue
            self._handles.append((kernel32, handle))
            thread = threading.Thread(
                target=self._watch, args=(kernel32, handle, folder),
                daemon=True,
                name="quickfind-fresh-{}".format(os.path.basename(folder)))
            thread.start()
            self._threads.append(thread)
        return bool(self._threads)

    def stop(self) -> None:
        self._stop.set()
        for kernel32, handle in self._handles:
            try:
                # Cancel first: closing a handle with a blocking read still
                # pending on another thread is not enough to wake it reliably.
                kernel32.CancelIoEx(handle, None)
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        self._handles = []

    # -- the watch loop ----------------------------------------------------

    def _watch(self, kernel32, handle, folder) -> None:
        buffer = ctypes.create_string_buffer(BUFFER_SIZE)
        returned = wintypes.DWORD(0)
        while not self._stop.is_set():
            ok = kernel32.ReadDirectoryChangesW(
                handle, buffer, BUFFER_SIZE, True, NOTIFY_FILTER,
                ctypes.byref(returned), None, None)
            if not ok or self._stop.is_set():
                return
            if returned.value == 0:
                # The buffer overflowed and the changes are simply gone.
                if self.on_overflow is not None:
                    self._safely(self.on_overflow)
                continue
            for relative, removed in self._parse(buffer.raw, returned.value):
                if self._ignored(relative):
                    continue
                path = os.path.join(folder, relative)
                # Stat here, on this thread. The UI thread would otherwise pay
                # for it, once per file, in the middle of a burst.
                is_dir, mtime = _describe(path)
                if not removed and mtime is None:
                    continue        # gone again already
                self._safely(self.on_change, path, removed, is_dir, mtime or 0)

    @staticmethod
    def _parse(raw: bytes, size: int):
        """Walk the FILE_NOTIFY_INFORMATION chain."""
        offset = 0
        while offset + 12 <= size:
            next_offset, action, name_bytes = struct.unpack_from(
                "<III", raw, offset)
            start = offset + 12
            end = start + name_bytes
            if name_bytes < 0 or end > size:
                break
            name = raw[start:end].decode("utf-16-le", "replace")
            if action in (FILE_ACTION_ADDED, FILE_ACTION_RENAMED_NEW_NAME):
                yield name, False
            elif action in (FILE_ACTION_REMOVED, FILE_ACTION_RENAMED_OLD_NAME):
                yield name, True
            if next_offset == 0:
                break
            offset += next_offset

    def _ignored(self, relative: str) -> bool:
        parts = relative.split("\\")
        if is_partial(parts[-1]):
            return True
        return any(part.lower() in self.excludes for part in parts)

    def _safely(self, call, *args) -> None:
        """A bad callback must not kill the watch, but must not vanish either."""
        try:
            call(*args)
        except Exception as exc:
            self.error = "callback failed: {}".format(exc)
