import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qf import tray


def recorder():
    seen = []
    return seen, lambda tag: (lambda: seen.append(tag))


class TestMessageDispatch(unittest.TestCase):
    """The WndProc routes messages without needing a real window."""

    def setUp(self):
        self.seen = []
        self.icon = tray.TrayIcon(
            on_show=lambda: self.seen.append("show"),
            on_reindex=lambda: self.seen.append("reindex"),
            on_quit=lambda: self.seen.append("quit"),
        )

    def send(self, message, wparam=0, lparam=0):
        return self.icon._handle_message(None, message, wparam, lparam)

    def test_left_click_shows(self):
        self.send(tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)
        self.assertEqual(self.seen, ["show"])

    def test_double_click_shows(self):
        self.send(tray.WM_TRAYICON, 0, tray.WM_LBUTTONDBLCLK)
        self.assertEqual(self.seen, ["show"])

    def test_menu_show_command(self):
        self.send(tray.WM_COMMAND, tray.ID_SHOW, 0)
        self.assertEqual(self.seen, ["show"])

    def test_menu_reindex_command(self):
        self.send(tray.WM_COMMAND, tray.ID_REINDEX, 0)
        self.assertEqual(self.seen, ["reindex"])

    def test_menu_quit_command(self):
        self.send(tray.WM_COMMAND, tray.ID_QUIT, 0)
        self.assertEqual(self.seen, ["quit"])

    def test_unknown_command_is_ignored(self):
        self.send(tray.WM_COMMAND, 9999, 0)
        self.assertEqual(self.seen, [])

    def test_high_word_of_lparam_is_ignored(self):
        # Windows packs extra data in the high word; only the low word is the event.
        self.send(tray.WM_TRAYICON, 0, (17 << 16) | tray.WM_LBUTTONUP)
        self.assertEqual(self.seen, ["show"])

    def test_callback_exception_does_not_escape(self):
        icon = tray.TrayIcon(on_show=lambda: 1 / 0, on_reindex=lambda: None,
                             on_quit=lambda: None)
        icon._handle_message(None, tray.WM_TRAYICON, 0, tray.WM_LBUTTONUP)

    def test_wndproc_reference_is_retained(self):
        # Windows holds a raw pointer; losing this reference crashes the process.
        self.assertIsNotNone(self.icon._wndproc)


class TestTrayLifecycle(unittest.TestCase):
    """Actually register an icon with the shell, then remove it."""

    def test_start_and_stop(self):
        seen = []
        icon = tray.TrayIcon(lambda: seen.append("show"), lambda: None,
                             lambda: None, tooltip="QuickFind test")
        started = icon.start(timeout=8)
        try:
            self.assertTrue(started, f"tray icon failed: {icon.error}")
            self.assertIsNotNone(icon.hwnd)
            self.assertIsNone(icon.error)
        finally:
            icon.stop()

        for _ in range(50):
            if not icon._added:
                break
            time.sleep(0.1)
        self.assertFalse(icon._added, "icon was not removed from the tray")

    def test_pointer_sized_message_params_do_not_overflow(self):
        icon = tray.TrayIcon(lambda: None, lambda: None, lambda: None,
                             tooltip="QuickFind overflow test")
        self.assertTrue(icon.start(timeout=8), f"tray failed: {icon.error}")
        try:
            WM_NCHITTEST = 0x0084
            # Real windows receive pointer-sized values here; a 32-bit
            # prototype would raise OverflowError inside the callback.
            result = icon._handle_message(icon.hwnd, WM_NCHITTEST, 0,
                                          0x7FFFFFFFFFFF)
            self.assertIsInstance(result, int)
        finally:
            icon.stop()

    def test_stop_before_start_is_safe(self):
        tray.TrayIcon(lambda: None, lambda: None, lambda: None).stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
