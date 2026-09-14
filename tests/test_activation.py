"""Selection, keyboard activation and opening - the paths users actually hit."""
import gc
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

from qf import ui


class Row:
    def __init__(self, name, path, is_dir=False):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.score = 1.0
        self.index = 0
        self.fuzzy = False


class Stub:
    def __init__(self, rows):
        self.rows = rows
        self.commands = []

    def query(self, text):
        return (self.rows, "ok") if text.strip() else ([], "")

    def command(self, text):
        self.commands.append(text)


class Event:
    def __init__(self, y):
        self.y = y


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestSelectionSurvives(unittest.TestCase):
    """Regression: the entry's selection used to wipe the listbox's."""

    def setUp(self):
        self.rows = [Row("Downloads", r"C:\Users\me\Downloads", is_dir=True),
                     Row("a.txt", r"C:\a.txt")]
        self.app = make(lambda: ui.Launcher(Stub(self.rows)))

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def search(self, text="d"):
        self.app.entry.delete(0, "end")
        self.app.entry.insert(0, text)
        self.app._run_search()

    def test_tree_selection_is_independent_of_the_system_selection(self):
        # A Listbox tied its selection to the system selection, so the entry's
        # select_range silently cleared it. A Treeview has its own.
        self.assertEqual(str(self.app.tree.cget("selectmode")), "browse")

    def test_selection_survives_entry_select_range(self):
        self.search()
        self.app.entry.select_range(0, "end")
        self.assertEqual(self.app._selected(), 0)

    def test_selection_survives_show(self):
        self.search()
        self.app.show()
        self.assertEqual(self.app._selected(), 0,
                         "showing the window must not clear the highlighted row")

    def test_enter_still_has_a_target_after_show(self):
        self.search()
        self.app.show()
        # Existence is mocked so the fixture stays synthetic; without this the
        # test only passed because the path happened to exist on one machine.
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(ui, "open_path", return_value=True) as opener:
            self.app._on_return(None)
        opener.assert_called_once_with(self.rows[0].path)

    def test_folder_opens_on_enter(self):
        self.search()
        self.app.show()
        self.assertTrue(self.rows[0].is_dir)
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(ui, "open_path", return_value=True) as opener:
            self.app._on_return(None)
        opener.assert_called_once_with(r"C:\Users\me\Downloads")


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestBindingsAreGlobal(unittest.TestCase):
    """Clicking a row moves focus; activation keys must still work."""

    def setUp(self):
        self.app = make(lambda: ui.Launcher(Stub([Row("a.txt", r"C:\a.txt")])))

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def test_escape_bound_on_toplevel(self):
        self.assertTrue(self.app.root.bind("<Escape>"))

    def test_return_bound_on_toplevel(self):
        self.assertTrue(self.app.root.bind("<Return>"))

    def test_control_return_bound_on_toplevel(self):
        self.assertTrue(self.app.root.bind("<Control-Return>"))

    def test_numpad_enter_bound(self):
        self.assertTrue(self.app.root.bind("<KP_Enter>"))

    def test_not_scoped_to_the_entry(self):
        for sequence in ("<Return>", "<Escape>", "<Control-Return>"):
            self.assertFalse(self.app.entry.bind(sequence),
                             f"{sequence} must not be entry-scoped")

    def test_escape_hides_even_with_listbox_focused(self):
        self.app.show()
        self.app.entry.insert(0, "a")
        self.app._run_search()
        self.app.tree.focus_set()
        self.app.root.update()
        self.app.root.event_generate("<Escape>")
        self.app.root.update()
        self.assertFalse(self.app.is_visible())


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestMouse(unittest.TestCase):
    def setUp(self):
        self.rows = [Row(f"file{i}.txt", rf"C:\x\file{i}.txt") for i in range(6)]
        self.app = make(lambda: ui.Launcher(Stub(self.rows)))
        self.app.show()
        self.app.entry.insert(0, "file")
        self.app._run_search()
        self.app.root.update()

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def row_centre(self, index):
        items = self.app.tree.get_children()
        self.app.tree.see(items[index])
        self.app.root.update_idletasks()
        self.app.root.update()
        box = self.app.tree.bbox(items[index])
        self.assertTrue(box, f"row {index} is not laid out")
        return box[1] + box[3] // 2

    def test_hover_moves_the_selection(self):
        for target in (0, 2, 4, 1):
            self.app._on_hover(Event(self.row_centre(target)))
            self.assertEqual(self.app._selected(), target)

    def test_hovered_row_is_what_enter_opens(self):
        self.app._on_hover(Event(self.row_centre(3)))
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(ui, "open_path", return_value=True) as opener:
            self.app._on_return(None)
        opener.assert_called_once_with(self.rows[3].path)

    def test_hovered_row_is_what_ctrl_enter_reveals(self):
        self.app._on_hover(Event(self.row_centre(2)))
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(ui, "reveal_path", return_value=True) as revealer:
            self.app._on_reveal(None)
        revealer.assert_called_once_with(self.rows[2].path)

    def test_click_selects_and_returns_focus_to_the_entry(self):
        self.app._on_click(Event(self.row_centre(3)))
        self.assertEqual(self.app._selected(), 3)
        self.assertIs(self.app.root.focus_get(), self.app.entry)

    def test_hover_outside_rows_clamps(self):
        self.app._on_hover(Event(99999))
        self.assertLessEqual(self.app._selected(), len(self.rows) - 1)
        self.assertGreaterEqual(self.app._selected(), 0)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestMissingPaths(unittest.TestCase):
    def setUp(self):
        self.rows = [Row("gone.txt", r"C:\definitely\not\here.txt")]
        self.app = make(lambda: ui.Launcher(Stub(self.rows)))
        self.app.show()
        self.app.entry.insert(0, "gone")
        self.app._run_search()

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def test_reports_instead_of_failing_silently(self):
        with mock.patch.object(ui, "open_path") as opener:
            self.app._on_return(None)
        opener.assert_not_called()
        self.assertIn("No longer on disk", self.app.status.cget("text"))

    def test_window_stays_open_so_the_message_is_seen(self):
        self.app._on_return(None)
        self.assertTrue(self.app.is_visible())

    def test_reports_when_the_opener_fails(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(ui, "open_path", return_value=False):
            self.app._on_return(None)
        self.assertIn("Could not open", self.app.status.cget("text"))


class TestOpenerCommands(unittest.TestCase):
    def test_reveal_quotes_only_the_path(self):
        with mock.patch("subprocess.Popen") as popen:
            ui.reveal_path(r"C:\Program Files\Git\git-bash.exe")
        command = popen.call_args[0][0]
        self.assertIsInstance(command, str,
                              "must be one string; a list re-quotes the whole arg")
        self.assertEqual(command,
                         'explorer /select,"C:\\Program Files\\Git\\git-bash.exe"')

    def test_reveal_reports_failure(self):
        with mock.patch("subprocess.Popen", side_effect=OSError):
            self.assertFalse(ui.reveal_path(r"C:\x"))

    def test_open_uses_startfile(self):
        with mock.patch("os.startfile") as startfile:
            self.assertTrue(ui.open_path(r"C:\x\y.txt"))
        startfile.assert_called_once()

    def test_open_falls_back_to_reveal(self):
        with mock.patch("os.startfile", side_effect=OSError), \
             mock.patch.object(ui, "reveal_path", return_value=True) as revealer:
            self.assertTrue(ui.open_path(r"C:\x\y.txt"))
        revealer.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
