"""Reading a PDF in the window rather than squinting at the side pane."""
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
from _tkcheck import TK_AVAILABLE, make, run_search

from qf import shellicon, ui

# A one-pixel PNG, which is all any of this needs to be a picture.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
       b"\x00\x04\x00\x01\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82")


class Row:
    def __init__(self, path, is_dir=False):
        self.path = path
        self.name = os.path.basename(path)
        self.is_dir = is_dir


class Controller:
    rows_visible = 10

    def __init__(self, rows):
        self.rows = rows

    def query(self, text):
        return list(self.rows), "{} matches".format(len(self.rows))

    def command(self, text):
        pass


@unittest.skipUnless(TK_AVAILABLE, "no display")
class TestPageView(unittest.TestCase):
    def setUp(self):
        self.app = None

    def tearDown(self):
        if self.app is not None:
            try:
                self.app.shutdown()
                self.app.root.destroy()
            except Exception:
                pass
            self.app = None
        gc.collect()

    def launcher(self, rows):
        app = make(lambda: ui.Launcher(Controller(rows)))
        self.app = app
        app.root.unbind("<FocusOut>")
        app.show()
        run_search(app)
        # winfo_ismapped only tells the truth once Tk has laid the window out.
        app.root.update()
        return app

    def rows(self, *names):
        return [Row(os.path.join("C:\\docs", name)) for name in names]

    def settle(self, app, timeout=4.0):
        """Wait for the renderer to answer and hand the answer to the UI."""
        end = time.time() + timeout
        while app._done.empty() and time.time() < end:
            time.sleep(0.005)
        app._pump_id = None
        app._pump()
        app.root.update()

    # -- opening and closing -----------------------------------------------

    def test_a_pdf_opens_into_the_window(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
        self.assertEqual(app._page_path, os.path.join("C:\\docs", "contract.pdf"))
        self.assertTrue(app.page_view.winfo_ismapped())
        self.assertFalse(app.body.winfo_ismapped())

    def test_anything_that_is_not_a_pdf_is_left_alone(self):
        app = self.launcher(self.rows("holiday.mp4"))
        app._toggle_page_view()
        self.assertIsNone(app._page_path)
        self.assertTrue(app.body.winfo_ismapped())

    def test_the_same_key_puts_it_away_again(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
            app._toggle_page_view()
        app.root.update()
        self.assertIsNone(app._page_path)
        self.assertFalse(app.page_view.winfo_ismapped())
        self.assertTrue(app.body.winfo_ismapped())

    def test_escape_leaves_the_page_before_it_leaves_the_launcher(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
        app._on_escape()
        self.assertIsNone(app._page_path)
        self.assertTrue(app.is_visible())
        app._on_escape()
        self.assertFalse(app.is_visible())

    def test_typing_puts_the_page_away(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
        app.entry.insert("end", "x")
        app._on_key(type("E", (), {"keysym": "x"})())
        self.assertIsNone(app._page_path)

    def test_letting_go_of_ctrl_space_does_not_shut_it_again(self):
        # The page view opens on <Control-space>; the KeyRelease of the space
        # and of Ctrl itself then arrive at the search box. Neither changes
        # the query, so neither is a reason to put the page away.
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
            for released in ("space", "Control_L"):
                app._on_key(type("E", (), {"keysym": released})())
        self.assertEqual(app._page_path, os.path.join("C:\\docs", "contract.pdf"))

    def test_a_key_that_changes_nothing_does_not_search_again(self):
        app = self.launcher(self.rows("contract.pdf"))
        app.entry.insert(0, "report")
        app._on_key(type("E", (), {"keysym": "t"})())
        self.assertIsNotNone(app._after_id)
        app.root.after_cancel(app._after_id)
        app._after_id = None
        # Moving the caret leaves the query alone, so nothing is scheduled.
        app._on_key(type("E", (), {"keysym": "Left"})())
        self.assertIsNone(app._after_id)

    def test_dismissing_the_launcher_closes_the_page(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)):
            app._toggle_page_view()
            self.settle(app)
        app.hide()
        self.assertIsNone(app._page_path)

    # -- turning pages -----------------------------------------------------

    def test_right_and_left_turn_the_pages(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 3)) as render:
            app._toggle_page_view()
            self.settle(app)
            app._on_page_forward()
            self.settle(app)
            self.assertEqual(app._page_index, 1)
            app._on_page_forward()
            self.settle(app)
            self.assertEqual(app._page_index, 2)
            app._on_page_back()
            self.settle(app)
            self.assertEqual(app._page_index, 1)
        # The side pane renders page one through the same function, so count
        # only the renders asked for at the expanded view's size.
        box = app._page_box()
        asked = [call.args[1] for call in render.call_args_list
                 if call.kwargs.get("box") == box]
        self.assertEqual(asked, [0, 1, 2, 1])

    def test_it_will_not_page_past_either_end(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 2)):
            app._toggle_page_view()
            self.settle(app)
            app._on_page_back()
            self.assertEqual(app._page_index, 0)
            app._on_page_forward()
            self.settle(app)
            app._on_page_forward()
            self.assertEqual(app._page_index, 1)

    def test_a_single_page_document_says_nothing_about_pages(self):
        app = self.launcher(self.rows("receipt.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 1)):
            app._toggle_page_view()
            self.settle(app)
        self.assertNotIn("of", app.page_foot.cget("text"))
        self.assertIn("Esc", app.page_foot.cget("text"))

    def test_a_multi_page_document_says_where_it_is(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 7)):
            app._toggle_page_view()
            self.settle(app)
            app._on_page_forward()
            self.settle(app)
        self.assertIn("2 of 7", app.page_foot.cget("text"))

    def test_the_arrows_are_left_to_the_entry_when_no_page_is_open(self):
        # Left and Right move the caret in the search box; the page view may
        # only take them while it is actually up.
        app = self.launcher(self.rows("contract.pdf"))
        self.assertIsNone(app._on_page_forward())
        self.assertIsNone(app._on_page_back())

    # -- what the renderer is asked for -------------------------------------

    def test_the_page_is_drawn_to_fit_the_window(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 1)) as render:
            app._toggle_page_view()
            self.settle(app)
        # The side pane renders page one through the same function, and its
        # call can be the last one, so ask whether the expanded view's size
        # was requested rather than what the most recent request happened to be.
        boxes = [call.kwargs.get("box") for call in render.call_args_list]
        self.assertIn(app._page_box(), boxes)
        width, height = app._page_box()
        # Far larger than the side pane could ever give it, which is the
        # entire point of expanding.
        self.assertGreater(width, app.preview_width)
        self.assertGreater(height, app.px(ui.PREVIEW_PAGE) * 2)

    def test_a_page_that_will_not_render_says_so(self):
        app = self.launcher(self.rows("locked.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(None, 0)):
            app._toggle_page_view()
            self.settle(app)
        self.assertIn("cannot", app.page_label.cget("text"))

    def test_an_answer_for_a_page_already_turned_past_is_dropped(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 5)):
            app._toggle_page_view()
            self.settle(app)
        stale = app._page_token
        app._page_token += 1
        app._done.put(("page", stale, None, 99))
        app._pump_id = None
        app._pump()
        # The stale answer claimed 99 pages; it was ignored.
        self.assertEqual(app._page_count, 5)

    # -- the window while a page is up --------------------------------------

    def test_the_window_takes_the_shape_of_the_page(self):
        app = self.launcher(self.rows("contract.pdf"))
        listed = app._window_width()
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 1)):
            app._toggle_page_view()
            self.settle(app)
        # The stub page is one pixel wide, so the window shrinks to its floor
        # rather than staying as wide as a results list.
        self.assertLess(app._window_width(), listed)
        self.assertGreaterEqual(app._window_width(), app.px(ui.MIN_WIDTH))

    def test_a_page_never_reaches_under_the_taskbar(self):
        app = self.launcher(self.rows("contract.pdf"))
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 1)):
            app._toggle_page_view()
            self.settle(app)
        app.root.update_idletasks()
        top, usable = ui.work_area(app.root.winfo_screenheight())
        self.assertGreaterEqual(app.root.winfo_rooty(), top)
        self.assertLessEqual(app.root.winfo_rooty() + app.root.winfo_height(),
                             top + usable)

    def test_a_pdf_page_count_is_not_clipped_off_the_pane(self):
        # The pane has a fixed height so that it still has a shape when a
        # search returns two rows. A PDF's details run to four lines -- type,
        # size, date and page count -- and the last of them used to fall off
        # the bottom.
        app = self.launcher(self.rows("contract.pdf"))
        # A real page thumbnail, not the one-pixel stub: the whole question is
        # whether the details still fit underneath one.
        wide = app.px(ui.PREVIEW_THUMB)
        tall = int(wide * 1.29)          # the proportions of a printed page
        page = shellicon.encode_png(wide, tall, b"\xff" * (wide * tall * 4))
        app._show_preview({
            "path": os.path.join("C:\\docs", "contract.pdf"),
            "image": page,
            "is_thumbnail": True,
            "meta": "Document\n362.5 KB\n19 Sep 2024  03:04\n7 pages",
            "excerpt": "",
            "is_video": False,
            "is_pdf": True,
        })
        app.root.update_idletasks()
        self.assertTrue(app.preview_hint.winfo_ismapped())
        last = app.preview_hint
        bottom = last.winfo_y() + last.winfo_reqheight()
        self.assertLessEqual(bottom, app.preview.winfo_reqheight())

    def test_the_selection_holds_still_behind_the_page(self):
        app = self.launcher(self.rows("a.pdf", "b.pdf", "c.pdf"))
        app._select(1)
        with mock.patch.object(ui.pdfpreview, "render_page",
                               return_value=(PNG, 1)):
            app._toggle_page_view()
            self.settle(app)
            app._move(1)
        self.assertEqual(app._selected(), 1)


@unittest.skipUnless(TK_AVAILABLE, "no display")
class TestWorkArea(unittest.TestCase):
    def test_it_is_no_taller_than_the_screen(self):
        top, usable = ui.work_area(1080)
        self.assertGreater(usable, 0)
        self.assertGreaterEqual(top, 0)

    def test_a_refusal_falls_back_to_the_whole_screen(self):
        with mock.patch.object(ui.ctypes, "windll") as windll:
            windll.user32.SystemParametersInfoW.return_value = 0
            self.assertEqual(ui.work_area(1234), (0, 1234))


if __name__ == "__main__":
    unittest.main()
