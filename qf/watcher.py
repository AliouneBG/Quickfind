"""Watch the NTFS change journal so the index does not go stale.

The journal is read with a blocking call, so an idle machine costs nothing --
no polling loop, no periodic rescan. Only creates, deletes and renames are
requested; ordinary writes do not change what the index holds.

This reports *that* the volume changed, not what to patch. With MFT enumeration
a full rebuild takes a couple of seconds, so rebuilding on a debounce is both
simpler and less error-prone than mutating the packed index in place.
"""
from __future__ import annotations

import ctypes
import struct
import threading
from ctypes import wintypes

from . import fsindex

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_RENAME_OLD_NAME = 0x00001000

STRUCTURAL_CHANGES = (USN_REASON_FILE_CREATE | USN_REASON_FILE_DELETE
                      | USN_REASON_RENAME_NEW_NAME | USN_REASON_RENAME_OLD_NAME)

WAIT_SECONDS = 3
BUFFER_SIZE = 1 << 16


class UsnWatcher:
    """Signals ``on_change`` when files appear, vanish or are renamed."""

    def __init__(self, drives, on_change):
        self.drives = [d.rstrip("\\/:")[:1].upper() for d in drives]
        self.on_change = on_change
        self._threads = []
        self._stop = threading.Event()
        self._handles = []
        self.error = None

    def available(self) -> bool:
        return fsindex.is_admin() and bool(self.drives)

    def start(self) -> bool:
        if not self.available():
            self.error = "requires administrator"
            return False
        started = False
        for letter in self.drives:
            try:
                kernel32, handle = fsindex.open_volume(letter)
                journal_id, next_usn = self._query(kernel32, handle)
            except OSError as exc:
                self.error = str(exc)
                continue
            self._handles.append((kernel32, handle))
            thread = threading.Thread(
                target=self._watch, args=(kernel32, handle, journal_id, next_usn),
                daemon=True, name=f"quickfind-usn-{letter}")
            thread.start()
            self._threads.append(thread)
            started = True
        return started

    @staticmethod
    def _query(kernel32, handle):
        out = ctypes.create_string_buffer(56)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle, FSCTL_QUERY_USN_JOURNAL, None, 0,
            out, ctypes.sizeof(out), ctypes.byref(returned), None)
        if not ok:
            raise OSError(ctypes.get_last_error(), "FSCTL_QUERY_USN_JOURNAL failed")
        journal_id, _first, next_usn = struct.unpack_from("<Qqq", out.raw, 0)
        return journal_id, next_usn

    def _watch(self, kernel32, handle, journal_id, next_usn) -> None:
        buf = ctypes.create_string_buffer(BUFFER_SIZE)
        returned = wintypes.DWORD(0)

        while not self._stop.is_set():
            request = struct.pack("<qIIQQQ", next_usn, STRUCTURAL_CHANGES, 0,
                                  WAIT_SECONDS, 1, journal_id)
            ok = kernel32.DeviceIoControl(
                handle, FSCTL_READ_USN_JOURNAL, request, len(request),
                buf, ctypes.sizeof(buf), ctypes.byref(returned), None)
            if not ok:
                # The journal can be deleted or recreated underneath us.
                break
            size = returned.value
            if size < 8:
                continue
            raw = buf.raw
            next_usn = struct.unpack_from("<q", raw, 0)[0]

            changes = 0
            offset = 8
            while offset < size:
                rec_len = struct.unpack_from("<I", raw, offset)[0]
                if rec_len == 0 or offset + rec_len > size:
                    break
                changes += 1
                offset += rec_len

            if changes and not self._stop.is_set():
                try:
                    self.on_change(changes)
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        for kernel32, handle in self._handles:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        self._handles = []
