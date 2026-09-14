"""Single-instance guard.

A second launch should not silently fail to grab the hotkey; it should wake the
instance that already owns it and exit. A named mutex detects the duplicate and
a named event carries the "show yourself" signal, so the running process needs
no window or socket to be reachable.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258

MUTEX_NAME = "Local\\QuickFind-SingleInstance"
EVENT_NAME = "Local\\QuickFind-Show"


class SingleInstance:
    def __init__(self, mutex_name=MUTEX_NAME, event_name=EVENT_NAME):
        self.mutex_name = mutex_name
        self.event_name = event_name
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex = self._kernel32.CreateMutexW(None, False, mutex_name)
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        self._event = None
        self._thread = None
        self._stop = threading.Event()

    def signal_existing(self) -> bool:
        """Ask the instance that already holds the mutex to show itself."""
        handle = self._kernel32.OpenEventW(EVENT_MODIFY_STATE, False,
                                           self.event_name)
        if not handle:
            return False
        try:
            return bool(self._kernel32.SetEvent(handle))
        finally:
            self._kernel32.CloseHandle(handle)

    def listen(self, callback) -> None:
        """Run ``callback`` whenever another launch signals us."""
        self._event = self._kernel32.CreateEventW(None, False, False,
                                                  self.event_name)
        if not self._event:
            return
        self._thread = threading.Thread(target=self._wait_loop, args=(callback,),
                                        daemon=True, name="quickfind-instance")
        self._thread.start()

    def _wait_loop(self, callback) -> None:
        while not self._stop.is_set():
            # A timeout rather than INFINITE so close() is always observed.
            result = self._kernel32.WaitForSingleObject(self._event, 500)
            if result == WAIT_OBJECT_0 and not self._stop.is_set():
                try:
                    callback()
                except Exception:
                    pass

    def close(self) -> None:
        self._stop.set()
        for handle in (self._event, self._mutex):
            if handle:
                try:
                    self._kernel32.CloseHandle(handle)
                except Exception:
                    pass
        self._event = None
        self._mutex = None
