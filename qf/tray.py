"""Notification-area icon with a right-click menu.

Shell_NotifyIcon delivers its callbacks as window messages, so this owns a
message-only window and pumps it on its own thread. Callbacks fire on that
thread -- they must only hand work to the UI thread, never touch Tk directly.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_QUIT = 0x0012

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_SHARED = 0x8000

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800

HWND_MESSAGE = -3

ID_SHOW = 1001
ID_REINDEX = 1002
ID_QUIT = 1003

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uVersion", ctypes.c_uint),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class TrayIcon:
    def __init__(self, on_show, on_reindex, on_quit, tooltip="QuickFind"):
        self.on_show = on_show
        self.on_reindex = on_reindex
        self.on_quit = on_quit
        self.tooltip = tooltip
        self.hwnd = None
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._added = False
        self.error = None
        # The WNDPROC and WNDCLASS must outlive the window: Windows keeps raw
        # pointers to both, and letting Python collect them crashes the process.
        self._wndproc = WNDPROC(self._handle_message)
        self._wndclass = None

    def start(self, timeout=5.0) -> bool:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="quickfind-tray")
        self._thread.start()
        self._ready.wait(timeout)
        return self._added

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._user32 = user32
        self._shell32 = shell32
        self._thread_id = kernel32.GetCurrentThreadId()

        # Without explicit prototypes ctypes assumes 32-bit ints, and the
        # pointer-sized wParam/lParam of messages like WM_NCCREATE overflow.
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                          WPARAM, LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        WPARAM, LPARAM]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                              WPARAM, LPARAM]
        user32.AppendMenuW.argtypes = [wintypes.HMENU, ctypes.c_uint,
                                       ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.restype = ctypes.c_int

        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = hinst
            wc.lpszClassName = f"QuickFindTray{self._thread_id}"
            self._wndclass = wc
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassW failed")

            user32.CreateWindowExW.restype = wintypes.HWND
            self.hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, "QuickFind", 0, 0, 0, 0, 0,
                wintypes.HWND(HWND_MESSAGE), None, hinst, None)
            if not self.hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")

            user32.LoadImageW.restype = wintypes.HICON
            icon = user32.LoadImageW(None, wintypes.LPCWSTR(IDI_APPLICATION),
                                     IMAGE_ICON, 0, 0, LR_SHARED)
            if not icon:
                icon = user32.LoadIconW(None, IDI_APPLICATION)

            data = NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            data.hWnd = self.hwnd
            data.uID = 1
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            data.uCallbackMessage = WM_TRAYICON
            data.hIcon = icon
            data.szTip = self.tooltip
            self._data = data
            if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
                raise OSError(ctypes.get_last_error(), "Shell_NotifyIconW failed")
            self._added = True
        except Exception as exc:
            self.error = exc
            self._ready.set()
            return

        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._remove_icon()

    def _handle_message(self, hwnd, message, wparam, lparam):
        if message == WM_TRAYICON:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._safe(self.on_show)
            elif event == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if message == WM_COMMAND:
            command = wparam & 0xFFFF
            if command == ID_SHOW:
                self._safe(self.on_show)
            elif command == ID_REINDEX:
                self._safe(self.on_reindex)
            elif command == ID_QUIT:
                self._safe(self.on_quit)
            return 0
        if message == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    @staticmethod
    def _safe(callback) -> None:
        try:
            callback()
        except Exception:
            pass

    def _show_menu(self) -> None:
        user32 = self._user32
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, MF_STRING, ID_SHOW, "Show  (Alt+Space)")
            user32.AppendMenuW(menu, MF_STRING, ID_REINDEX, "Rebuild index")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Quit QuickFind")
            point = POINT()
            user32.GetCursorPos(ctypes.byref(point))
            # Required so the menu dismisses when the user clicks elsewhere.
            user32.SetForegroundWindow(self.hwnd)
            choice = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD, point.x, point.y,
                0, self.hwnd, None)
            if choice:
                user32.PostMessageW(self.hwnd, WM_COMMAND, choice, 0)
        finally:
            user32.DestroyMenu(menu)

    def _remove_icon(self) -> None:
        if self._added:
            try:
                self._shell32.Shell_NotifyIconW(NIM_DELETE,
                                                ctypes.byref(self._data))
            except Exception:
                pass
            self._added = False

    def stop(self) -> None:
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id,
                                                        WM_QUIT, 0, 0)
            except Exception:
                pass
