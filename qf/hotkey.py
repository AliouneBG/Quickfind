"""System-wide hotkey via RegisterHotKey, driven by its own message loop.

RegisterHotKey delivers WM_HOTKEY to the thread that registered it, so the
listener owns a dedicated thread and a GetMessage pump.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN,
}

NAMED_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "insert": 0x2D,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "`": 0xC0, "tilde": 0xC0, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE,
    "/": 0xBF, "\\": 0xDC, "[": 0xDB, "]": 0xDD, "-": 0xBD, "=": 0xBB,
}
for _n in range(1, 25):
    NAMED_KEYS[f"f{_n}"] = 0x6F + _n


class HotkeyError(RuntimeError):
    pass


def parse(spec: str):
    """Turn ``"ctrl+shift+space"`` into ``(modifier_mask, virtual_key)``."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise HotkeyError(f"empty hotkey: {spec!r}")

    mods = 0
    key = None
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise HotkeyError(f"more than one non-modifier key in {spec!r}")

    if key is None:
        raise HotkeyError(f"no key in {spec!r}, only modifiers")
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    else:
        raise HotkeyError(f"unknown key {key!r} in {spec!r}")

    # Windows owns Alt+Tab and Ctrl+Alt+Del; RegisterHotKey will not yield them.
    if vk == 0x09 and mods & MOD_ALT:
        raise HotkeyError(
            "Alt+Tab is reserved by Windows and cannot be reassigned. "
            "Try alt+space or ctrl+space."
        )
    return mods, vk


class HotkeyListener:
    def __init__(self, spec: str, callback):
        self.spec = spec
        self.mods, self.vk = parse(spec)
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="quickfind-hotkey")
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        registered = user32.RegisterHotKey(
            None, 1, self.mods | MOD_NOREPEAT, self.vk
        )
        if not registered:
            err = ctypes.get_last_error()
            hint = " (already held by another application)" if err == 1409 else ""
            self._error = HotkeyError(
                f"could not register {self.spec!r}{hint} [error {err}]"
            )
            self._ready.set()
            return

        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
