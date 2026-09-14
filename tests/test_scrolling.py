"""Scrolling the results: how far a notch goes, and where the list sits."""
import gc
import os
import sys
import unittest

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
        self.note = "note"

    def query(self, text):
        return (self.rows, self.note) if text.strip() else ([], "")

    def command(self, text):
        pass


class Wheel:
    """A mouse wheel event. Positive delta is a push away from you, scroll up."""

    def __init__(self, delta):
        self.delta = delta
        self.x = 10
        self.y = 10


class TestWheelLines(unittest.TestCase):
    def test_a_sensible_number_of_rows(self):
        lines = ui.wheel_lines()
        self.assertGreaterEqual(lines, 1)
        self.assertLessEqual(lines, ui.MAX_WHEEL_LINES)

    def test_more_than_the_single_row_tk_would_scroll(self):
        # Tk's own Treeview binding scrolls exactly one row per notch, which
        # is what made the list feel slow to get through.
        self.assertGreater(ui.wheel_lines(), 1)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestScrolling(unittest.TestCase):
    def rows(self, count, prefix="note"):
        return [Row("{}{:03d}.txt".format(prefix, n),
                    r"C:\bucket\{}{:03d}.txt".format(prefix, n))
                for n in range(count)]

    def launcher(self, rows, page_rows=ui.PAGE_ROWS):
        app = make(lambda: ui.Launcher(Stub(rows), page_rows=page_rows))
        self.addCleanup(self.close, app)
        app.show()
        app.entry.delete(0, "end")
        app.entry.insert(0, "note")
        app._run_search()
        return app

    def close(self, app):
        try:
            app.shutdown()
            app.root.destroy()
        except Exception:
            pass
        gc.collect()

    def test_only_the_first_page_is_rendered(self):
        app = self.launcher(self.rows(300), page_rows=40)
        self.assertEqual(len(app.tree.get_children()), 40)

    def test_everything_is_rendered_when_it_fits(self):
        app = self.launcher(self.rows(12), page_rows=40)
        self.assertEqual(len(app.tree.get_children()), 12)

    def test_scrolling_to_the_end_brings_more(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app.tree.yview_moveto(1.0)
        app.root.update_idletasks()
        app._on_wheel(Wheel(-120))
        self.assertGreater(len(app.tree.get_children()), 40)

    def test_pages_keep_coming_until_the_results_run_out(self):
        app = self.launcher(self.rows(90), page_rows=40)
        for _ in range(10):
            app.tree.yview_moveto(1.0)
            app.root.update_idletasks()
            app._on_wheel(Wheel(-120))
        self.assertEqual(len(app.tree.get_children()), 90)

    def test_no_more_pages_than_there_are_results(self):
        app = self.launcher(self.rows(45), page_rows=40)
        for _ in range(5):
            self.assertTrue(app._extend_rows() or True)
        self.assertEqual(len(app.tree.get_children()), 45)

    def test_extending_reports_whether_it_did_anything(self):
        app = self.launcher(self.rows(45), page_rows=40)
        self.assertTrue(app._extend_rows())
        self.assertFalse(app._extend_rows())

    def test_scrolling_up_near_the_top_adds_nothing(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app._on_wheel(Wheel(120))
        self.assertEqual(len(app.tree.get_children()), 40)

    def test_a_notch_moves_several_rows(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app.tree.yview_moveto(0)
        app.root.update_idletasks()
        before = app.tree.yview()[0]
        app._on_wheel(Wheel(-120))
        app.root.update_idletasks()
        moved = (app.tree.yview()[0] - before) * len(app.tree.get_children())
        self.assertAlmostEqual(moved, ui.wheel_lines(), delta=1)

    def test_the_wheel_does_not_also_run_tks_own_binding(self):
        app = self.launcher(self.rows(300), page_rows=40)
        self.assertEqual(app._on_wheel(Wheel(-120)), "break")

    def test_arrowing_past_the_last_row_brings_more(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app._select(39)
        app._move(1)
        self.assertGreater(len(app.tree.get_children()), 40)
        self.assertEqual(app._selected(), 40)

    def test_a_new_search_scrolls_back_to_the_top(self):
        # Searching again while scrolled down used to leave the view where it
        # was, so the match that had just been selected was off screen.
        app = self.launcher(self.rows(300), page_rows=40)
        app.tree.yview_moveto(1.0)
        app.root.update_idletasks()
        self.assertGreater(app.tree.yview()[0], 0.0)
        app.controller.rows = self.rows(300, prefix="other")
        app._run_search()
        app.root.update_idletasks()
        self.assertEqual(app.tree.yview()[0], 0.0)

    def test_a_new_search_selects_the_first_row(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app._select(30)
        app.controller.rows = self.rows(300, prefix="other")
        app._run_search()
        self.assertEqual(app._selected(), 0)

    def test_a_new_search_shows_one_page_again(self):
        app = self.launcher(self.rows(300), page_rows=40)
        app._extend_rows()
        app._extend_rows()
        self.assertEqual(len(app.tree.get_children()), 120)
        app._run_search()
        self.assertEqual(len(app.tree.get_children()), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
