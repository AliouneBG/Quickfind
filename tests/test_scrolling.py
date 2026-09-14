"""The results list is a viewport: a few widgets over however many results."""
import gc
import os
import sys
import time
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
        self.note = "note"

    def query(self, text):
        return (self.rows, self.note) if text.strip() else ([], "")

    def command(self, text):
        pass


class Wheel:
    """A wheel event. A negative delta is a pull toward you: scroll down."""

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
class TestViewport(unittest.TestCase):
    def rows(self, count, prefix="note"):
        return [Row("{}{:05d}.txt".format(prefix, n),
                    r"C:\bucket\{}{:05d}.txt".format(prefix, n))
                for n in range(count)]

    def launcher(self, rows):
        app = make(lambda: ui.Launcher(Stub(rows)))
        self.addCleanup(self.close, app)
        app.show()
        app.entry.delete(0, "end")
        app.entry.insert(0, "note")
        app._run_search()
        app.root.update()
        return app

    def close(self, app):
        try:
            app.shutdown()
            app.root.destroy()
        except Exception:
            pass
        gc.collect()

    def shown(self, app):
        return [app.tree.item(item, "text")
                for item in app.tree.get_children()]

    # -- how many widgets exist -------------------------------------------

    def test_only_a_screenful_of_widgets_exists(self):
        app = self.launcher(self.rows(50_000))
        self.assertEqual(len(app.tree.get_children()), app.rows_visible)

    def test_the_whole_result_set_is_still_the_list(self):
        app = self.launcher(self.rows(50_000))
        self.assertEqual(app.row_count(), 50_000)

    def test_a_short_result_set_uses_fewer_widgets(self):
        app = self.launcher(self.rows(3))
        self.assertEqual(len(app.tree.get_children()), 3)

    def test_widgets_are_reused_rather_than_rebuilt(self):
        app = self.launcher(self.rows(50_000))
        before = list(app.tree.get_children())
        app._scroll_to(20_000)
        self.assertEqual(list(app.tree.get_children()), before)

    # -- scrolling ---------------------------------------------------------

    def test_scrolling_changes_which_results_are_shown(self):
        app = self.launcher(self.rows(50_000))
        first = self.shown(app)
        app._scroll_to(1000)
        self.assertNotEqual(self.shown(app), first)
        self.assertIn("note01000", self.shown(app)[0])

    def test_a_wheel_notch_moves_the_configured_number_of_rows(self):
        app = self.launcher(self.rows(1000))
        app._on_wheel(Wheel(-120))
        self.assertEqual(app._top, ui.wheel_lines())

    def test_scrolling_up_stops_at_the_top(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(5)
        for _ in range(10):
            app._on_wheel(Wheel(120))
        self.assertEqual(app._top, 0)

    def test_scrolling_down_stops_at_the_end(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(10_000)
        self.assertEqual(app._top, 1000 - app.rows_visible)

    def test_the_last_row_is_reachable(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(app._max_top())
        self.assertIn("note00999", self.shown(app)[-1])

    def test_scrolling_is_quick_even_at_the_far_end(self):
        app = self.launcher(self.rows(50_000))
        start = time.perf_counter()
        for n in range(20):
            app._scroll_to(n * 2000)
        elapsed = (time.perf_counter() - start) / 20 * 1000
        self.assertLess(elapsed, 15, "a scroll step should be a few ms")

    # -- the scrollbar -----------------------------------------------------

    def test_no_scrollbar_when_everything_fits(self):
        app = self.launcher(self.rows(4))
        self.assertFalse(app.scrollbar.winfo_ismapped())

    def test_a_scrollbar_appears_when_there_is_more(self):
        app = self.launcher(self.rows(500))
        self.assertTrue(app.scrollbar.winfo_ismapped())

    def test_the_thumb_reflects_the_whole_result_set(self):
        app = self.launcher(self.rows(1000))
        first, last = app.scrollbar.get()
        self.assertAlmostEqual(first, 0.0, places=3)
        self.assertAlmostEqual(last, app.rows_visible / 1000, places=3)

    def test_the_thumb_follows_the_viewport(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(500)
        first, _last = app.scrollbar.get()
        self.assertAlmostEqual(first, 0.5, places=2)

    def test_dragging_the_thumb_moves_the_list(self):
        app = self.launcher(self.rows(1000))
        app._on_scrollbar("moveto", "0.25")
        self.assertEqual(app._top, 250)

    def test_paging_the_trough_moves_a_screenful(self):
        app = self.launcher(self.rows(1000))
        app._on_scrollbar("scroll", "1", "pages")
        self.assertEqual(app._top, app.rows_visible)

    def test_clicking_an_arrow_moves_one_row(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(10)
        app._on_scrollbar("scroll", "-1", "units")
        self.assertEqual(app._top, 9)

    # -- selection and the viewport ---------------------------------------

    def test_arrowing_down_drags_the_viewport(self):
        app = self.launcher(self.rows(1000))
        for _ in range(app.rows_visible + 2):
            app._move(1)
        self.assertEqual(app._selected(), app.rows_visible + 2)
        self.assertGreater(app._top, 0)
        self.assertLessEqual(app._top, app._selected())

    def test_the_selection_stays_visible_when_arrowing_back_up(self):
        app = self.launcher(self.rows(1000))
        app._select(500)
        for _ in range(app.rows_visible + 3):
            app._move(-1)
        self.assertLessEqual(app._top, app._selected())
        self.assertLess(app._selected(), app._top + app.rows_visible)

    def test_selecting_far_down_brings_it_into_view(self):
        app = self.launcher(self.rows(50_000))
        app._select(42_000)
        self.assertEqual(app._selected(), 42_000)
        self.assertLessEqual(app._top, 42_000)
        self.assertLess(42_000, app._top + app.rows_visible)

    def test_the_selection_survives_scrolling_away_and_back(self):
        app = self.launcher(self.rows(1000))
        app._select(5)
        app._scroll_to(700)
        self.assertEqual(app._selected(), 5)
        app._scroll_to(0)
        self.assertEqual(app._selected(), 5)

    def test_nothing_is_highlighted_when_the_selection_is_off_screen(self):
        app = self.launcher(self.rows(1000))
        app._select(5)
        app._scroll_to(700)
        self.assertEqual(app.tree.selection(), ())

    def test_hovering_picks_the_result_under_the_pointer(self):
        # The row Tk reports is an offset into the viewport, so it has to be
        # read as `top + row` rather than as a result index.
        app = self.launcher(self.rows(1000))
        app._scroll_to(300)
        third_row = app.tree.get_children()[2]
        with mock.patch.object(app.tree, "identify_row", return_value=third_row):
            app._on_hover(type("Event", (), {"y": 0})())
        self.assertEqual(app._selected(), 302,
                         "the third row on screen is the 303rd result")

    def test_hovering_past_the_last_row_selects_nothing_new(self):
        app = self.launcher(self.rows(1000))
        app._select(7)
        with mock.patch.object(app.tree, "identify_row", return_value=""):
            app._on_hover(type("Event", (), {"y": 9999})())
        self.assertEqual(app._selected(), 7)

    # -- a new search ------------------------------------------------------

    def test_a_new_search_returns_to_the_top(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(600)
        app.controller.rows = self.rows(1000, prefix="other")
        app._run_search()
        self.assertEqual(app._top, 0)
        self.assertEqual(app._selected(), 0)

    def test_a_new_search_repaints_the_rows(self):
        app = self.launcher(self.rows(1000))
        app._scroll_to(600)
        app.controller.rows = self.rows(1000, prefix="other")
        app._run_search()
        self.assertIn("other00000", self.shown(app)[0])

    def test_an_empty_search_empties_the_list(self):
        app = self.launcher(self.rows(1000))
        app.controller.rows = []
        app._run_search()
        self.assertEqual(app.tree.get_children(), ())
        self.assertEqual(app._selected(), -1)

    # -- filling the list out after the first paint ------------------------

    def test_the_list_grows_once_typing_pauses(self):
        """A keystroke fetches a screenful and then some; the rest follows.

        Resolving paths for two thousand matches costs about 10ms more per
        keystroke, and almost every query is refined again before anyone
        scrolls, so the long fetch waits for a pause.
        """
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: (self.rows(2000), "2000 matches")
        app._deepen()
        self.assertEqual(app.row_count(), 2000)

    def test_deepening_leaves_the_viewport_where_it_was(self):
        app = self.launcher(self.rows(400))
        app._scroll_to(200)
        app._select(205)
        app.controller.more = lambda text: (self.rows(2000), "2000 matches")
        app._deepen()
        self.assertEqual(app._top, 200)
        self.assertEqual(app._selected(), 205)

    def test_deepening_updates_the_status_line(self):
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: (self.rows(2000), "2000 matches")
        app._deepen()
        self.assertIn("2000 matches", app.status.cget("text"))

    # -- inviting a scroll only when there is one to make -------------------

    def test_a_long_list_invites_scrolling(self):
        app = self.launcher(self.rows(500))
        self.assertIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))

    def test_the_invitation_goes_away_at_the_end(self):
        # Twenty matches, and you have reached the twentieth: there is no
        # more, and saying so would be a lie.
        app = self.launcher(self.rows(20))
        self.assertIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))
        app._scroll_to(app._max_top())
        self.assertNotIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))

    def test_the_invitation_comes_back_on_the_way_up(self):
        app = self.launcher(self.rows(20))
        app._scroll_to(app._max_top())
        app._scroll_to(0)
        self.assertIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))

    def test_a_list_that_fits_never_invites_scrolling(self):
        app = self.launcher(self.rows(5))
        self.assertNotIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))

    def test_arrowing_to_the_last_row_drops_the_invitation(self):
        app = self.launcher(self.rows(20))
        for _ in range(25):
            app._move(1)
        self.assertEqual(app._selected(), 19)
        self.assertNotIn(ui.SCROLL_HINT.strip(), app.status.cget("text"))

    def test_the_count_itself_is_kept(self):
        app = self.launcher(self.rows(20))
        app._scroll_to(app._max_top())
        self.assertIn("note", app.status.cget("text"))

    def test_other_messages_are_left_alone(self):
        app = self.launcher(self.rows(500))
        app.set_status("Rebuilding index...")
        app._scroll_to(5)
        self.assertEqual(app.status.cget("text"), "Rebuilding index...")

    def test_rows_already_on_screen_are_never_disturbed(self):
        # A deeper search interleaves repeated names over a larger pool, so
        # its order differs from the quick one's. What is already on screen
        # stays exactly where it is; only genuinely new rows are added.
        app = self.launcher(self.rows(400))
        before = [r.path for r in app.results]
        reordered = list(reversed(self.rows(400))) + self.rows(600)[400:]
        app.controller.more = lambda text: (reordered, "600 matches")
        app._deepen()
        self.assertEqual([r.path for r in app.results[:400]], before)

    def test_a_shorter_answer_adds_nothing(self):
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: (self.rows(10), "10 matches")
        app._deepen()
        self.assertEqual(app.row_count(), 400)

    def test_the_same_file_is_never_listed_twice(self):
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: (self.rows(600), "600 matches")
        app._deepen()
        paths = [r.path for r in app.results]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(app.row_count(), 600)

    def test_nothing_to_add_changes_nothing(self):
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: None
        app._deepen()
        self.assertEqual(app.row_count(), 400)

    def test_a_controller_without_deepening_is_fine(self):
        app = self.launcher(self.rows(50))
        self.assertFalse(hasattr(app.controller, "more"))
        app._schedule_deepen()
        self.assertIsNone(app._deepen_id)

    def test_typing_again_cancels_the_pending_fetch(self):
        app = self.launcher(self.rows(400))
        app.controller.more = lambda text: (self.rows(2000), "2000 matches")
        app._schedule_deepen()
        self.assertIsNotNone(app._deepen_id)
        app._on_key(type("Event", (), {"keysym": "a"})())
        self.assertIsNone(app._deepen_id)

    def test_the_folder_column_does_not_shift_while_scrolling(self):
        # The column is measured once per search. Measuring it from whatever
        # is in view would make the folders jump sideways as the list moved.
        rows = self.rows(400) + [Row("a-very-much-longer-name-than-the-rest.txt",
                                     r"C:\bucket\a-very-much-longer-name.txt")]
        app = self.launcher(rows)
        before = app._column_px
        app._scroll_to(len(rows))
        self.assertEqual(app._column_px, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
