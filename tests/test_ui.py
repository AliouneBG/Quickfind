import gc
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

from qf import search, ui


class StubResult:
    def __init__(self, name, path, is_dir=False):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.score = 1.0
        self.index = 0


class StubController:
    def __init__(self, results=None, note="ok"):
        self.results = results or []
        self.note = note
        self.commands = []
        self.queries = []

    def query(self, text):
        self.queries.append(text)
        return (self.results, self.note) if text.strip() else ([], "")

    def command(self, text):
        self.commands.append(text)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestLauncher(unittest.TestCase):
    def setUp(self):
        self.results = [
            StubResult("report.txt", "C:\\Users\\me\\report.txt"),
            StubResult("reports", "C:\\Users\\me\\reports", is_dir=True),
            StubResult("draft.docx", "C:\\Users\\me\\reports\\draft.docx"),
        ]
        self.controller = StubController(self.results)
        self.app = make(lambda: ui.Launcher(self.controller))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()

    def type_query(self, text):
        self.app.entry.delete(0, "end")
        self.app.entry.insert(0, text)
        self.app._run_search()

    def test_results_render_one_row_each(self):
        self.type_query("report")
        self.assertEqual(self.app.row_count(), 3)
        self.assertIn("report.txt", self.app.row_text(0))

    def test_directories_are_distinguished_by_icon_not_text(self):
        # The "[dir]" prefix is gone; the shell folder icon carries that now.
        self.type_query("report")
        self.assertNotIn("[dir]", self.app.row_text(1))
        self.assertEqual(self.app._row_keys[1], "<dir>")
        self.assertNotEqual(self.app._row_keys[0], "<dir>")

    def test_rows_show_containing_folder(self):
        self.type_query("report")
        self.assertIn("C:\\Users\\me", self.app.row_text(0))

    def test_first_row_selected_by_default(self):
        self.type_query("report")
        self.assertEqual(self.app._selected(), 0)

    def test_empty_query_clears_rows(self):
        self.type_query("report")
        self.type_query("")
        self.assertEqual(self.app.row_count(), 0)

    def test_arrow_navigation_clamps(self):
        self.type_query("report")
        self.app._move(1)
        self.assertEqual(self.app._selected(), 1)
        self.app._move(10)
        self.assertEqual(self.app._selected(), 2)
        self.app._move(-99)
        self.assertEqual(self.app._selected(), 0)

    def test_navigation_without_results_is_safe(self):
        self.type_query("")
        self.assertEqual(self.app._move(1), "break")

    def test_colon_prefix_routes_to_command(self):
        self.app.entry.delete(0, "end")
        self.app.entry.insert(0, ":quit")
        self.app._on_return(None)
        self.assertEqual(self.controller.commands, [":quit"])

    def test_status_text_displayed(self):
        self.type_query("report")
        self.assertEqual(self.app.status.cget("text"), "ok")

    def test_toggle_visibility(self):
        self.assertFalse(self.app._visible)
        self.app.show()
        self.assertTrue(self.app._visible)
        self.app.toggle()
        self.assertFalse(self.app._visible)

    def test_hide_cancels_pending_search(self):
        self.app.show()
        self.app._after_id = self.app.root.after(10000, lambda: None)
        self.app.hide()
        self.assertIsNone(self.app._after_id)

    def test_panels_hidden_when_no_results(self):
        self.app.show()
        self.type_query("")
        self.assertFalse(self.app.body.winfo_ismapped())


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestLauncherWithRealSearcher(unittest.TestCase):
    """End-to-end: real index -> real searcher -> rendered rows."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_search import make_index, SAMPLE
        self.searcher = search.Searcher(make_index(SAMPLE))

        outer = self

        class RealController:
            def query(self, text):
                if not text.strip():
                    return [], ""
                found = outer.searcher.search(text, 40)
                return found, f"{len(found)} shown"

            def command(self, text):
                pass

        self.app = make(lambda: ui.Launcher(RealController()))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()

    def test_typing_progressively_narrows(self):
        counts = []
        for i in range(1, len("report.txt") + 1):
            self.app.entry.delete(0, "end")
            self.app.entry.insert(0, "report.txt"[:i])
            self.app._run_search()
            counts.append(self.app.row_count())
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertIn("report.txt", self.app.row_text(0))



@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestChrome(unittest.TestCase):
    def setUp(self):
        self.app = make(lambda: ui.Launcher(StubController([])))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()

    def test_starts_fully_transparent(self):
        self.assertEqual(float(self.app.root.attributes("-alpha")), 0.0)

    def test_fade_reaches_target_alpha(self):
        # Other Tk windows in the suite can steal focus mid-fade, and losing
        # focus legitimately dismisses the launcher.
        self.app.root.unbind("<FocusOut>")
        self.app.show()
        self.settle()
        self.assertAlmostEqual(
            float(self.app.root.attributes("-alpha")), self.app.alpha, places=2)

    def test_slide_offset_settles_at_zero(self):
        self.app.root.unbind("<FocusOut>")
        self.app.show()
        # The first frame runs synchronously, so the window is already on
        # screen, sitting below where it will come to rest.
        self.assertGreater(self.app._anim_offset, 0)
        self.assertLessEqual(self.app._anim_offset, self.app.px(ui.SLIDE_PX))
        self.settle()
        self.assertEqual(self.app._anim_offset, 0)

    def settle(self, timeout=2.0):
        """Run the loop until the window has finished animating."""
        end = time.time() + timeout
        while self.app._anim_id is not None and time.time() < end:
            self.app.root.update()
            time.sleep(0.005)
        self.app.root.update()

    def test_hiding_fades_rather_than_blinking_out(self):
        self.app.show()
        self.settle()
        self.app.hide()
        # Still on screen for a few milliseconds, on its way down.
        self.assertIsNotNone(self.app._anim_id)
        self.assertLess(float(self.app.root.attributes("-alpha")),
                        self.app.alpha)

    def test_hiding_ends_with_the_window_gone(self):
        self.app.show()
        self.settle()
        self.app.hide()
        self.settle()
        self.assertIsNone(self.app._anim_id)
        self.assertEqual(float(self.app.root.attributes("-alpha")), 0.0)
        self.assertEqual(self.app._anim_offset, 0)
        self.assertFalse(self.app.root.winfo_ismapped())

    def test_it_is_not_visible_the_moment_it_is_dismissed(self):
        # Whatever is still on screen, a dismissed launcher is not open: the
        # index refresh and the hotkey both ask this.
        self.app.show()
        self.app.hide()
        self.assertFalse(self.app.is_visible())

    def test_reopening_mid_fade_brings_it_back(self):
        self.app.show()
        self.settle()
        self.app.hide()
        self.app.show()
        self.settle()
        self.assertTrue(self.app.is_visible())
        self.assertAlmostEqual(float(self.app.root.attributes("-alpha")),
                               self.app.alpha, places=2)

    def test_closing_sinks_back_the_way_it_came(self):
        self.app.root.unbind("<FocusOut>")
        self.app.show()
        self.settle()
        self.app.hide()
        self.app.root.update()
        # Part way down, never further than the drift allows, and back to the
        # resting offset once the window is gone.
        self.assertGreaterEqual(self.app._anim_offset, 0)
        self.assertLessEqual(self.app._anim_offset, self.app.px(ui.CLOSE_DRIFT_PX))
        self.settle()
        self.assertEqual(self.app._anim_offset, 0)

    def test_opening_takes_about_as_long_as_it_says(self):
        self.app.root.unbind("<FocusOut>")
        started = time.monotonic()
        self.app.show()
        self.settle()
        elapsed = (time.monotonic() - started) * 1000
        # Driven by the clock, not by counting frames: a late frame shortens
        # the remaining steps rather than stretching the whole animation.
        self.assertGreaterEqual(elapsed, ui.OPEN_MS * 0.5)
        self.assertLess(elapsed, ui.OPEN_MS + 120)

    def test_the_finer_timer_is_handed_back_after_animating(self):
        self.app.root.unbind("<FocusOut>")
        self.app.show()
        self.settle()
        self.app.hide()
        self.settle()
        self.assertFalse(self.app._anim_fine)
        self.assertEqual(self.app._fine_holds, 0)

    def test_reopening_mid_fade_hands_the_timer_back_once(self):
        # Cancelling an animation half way through must not leak the hold it
        # took, or the process keeps Windows on 1ms timers for ever.
        self.app.root.unbind("<FocusOut>")
        for _ in range(3):
            self.app.show()
            self.app.hide()
        self.app.show()
        self.settle()
        self.app.hide()
        self.settle()
        self.assertEqual(self.app._fine_holds, 0)

    def test_hiding_an_already_hidden_window_is_immediate(self):
        self.app.hide()
        self.assertIsNone(self.app._anim_id)
        self.assertEqual(float(self.app.root.attributes("-alpha")), 0.0)

    def test_opacity_defaults_to_module_value(self):
        self.assertEqual(self.app.alpha, ui.ALPHA)

    def test_opacity_is_configurable(self):
        app = make(lambda: ui.Launcher(StubController([]), opacity=0.7))
        try:
            self.assertAlmostEqual(app.alpha, 0.7)
        finally:
            app.shutdown()
            app.root.destroy()

    def test_opacity_is_clamped_to_usable_range(self):
        for given, expected in ((0.01, ui.MIN_ALPHA), (5.0, 1.0), (1.0, 1.0)):
            app = make(lambda: ui.Launcher(StubController([]), opacity=given))
            try:
                self.assertAlmostEqual(app.alpha, expected,
                                       msg=f"opacity {given} should clamp")
            finally:
                app.shutdown()
                app.root.destroy()
        gc.collect()

    def test_magnifier_is_drawn(self):
        self.assertEqual(len(self.app.glyph.find_all()), 2)

    def test_spinner_slot_reserved_while_idle(self):
        self.app.show()
        self.app.root.update()
        self.assertTrue(self.app.spinner.winfo_ismapped())
        self.assertEqual(self.app.spinner.find_all(), ())

    def test_spinner_draws_only_while_busy_and_visible(self):
        self.app.set_busy(True)
        self.assertEqual(self.app.spinner.find_all(), (), "hidden window must not spin")
        self.app.show()
        self.assertEqual(len(self.app.spinner.find_all()), 1)

    def test_spinner_advances(self):
        self.app.show()
        self.app.set_busy(True)
        first = self.app._spin_angle
        self.app._spin()
        self.assertNotEqual(self.app._spin_angle, first)

    def test_spinner_stops_when_not_busy(self):
        self.app.show()
        self.app.set_busy(True)
        self.app.set_busy(False)
        self.assertIsNone(self.app._spin_id)
        self.assertEqual(self.app.spinner.find_all(), ())

    def test_spinner_resumes_on_reshow(self):
        self.app.set_busy(True)
        self.app.show()
        self.assertIsNotNone(self.app._spin_id)
        self.app.hide()
        self.app.show()
        self.assertEqual(len(self.app.spinner.find_all()), 1)

    def test_shutdown_clears_every_pending_callback(self):
        self.app.show()
        self.app.set_busy(True)
        self.app._after_id = self.app.root.after(9000, lambda: None)
        self.app.shutdown()
        self.assertIsNone(self.app._anim_id)
        self.assertIsNone(self.app._spin_id)
        self.assertIsNone(self.app._after_id)




@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestRowFitting(unittest.TestCase):
    def setUp(self):
        self.app = make(lambda: ui.Launcher(StubController([])))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()

    def row_px(self, text):
        return self.app.row_font.measure(text)

    def budget(self):
        return self.app.width - 20

    def test_short_row_untouched(self):
        row = self.app._format_row(StubResult("a.txt", r"C:\tmp\a.txt"))
        self.assertIn("a.txt", row)
        self.assertIn(r"C:\tmp", row)
        self.assertNotIn(ui.ELLIPSIS, row)

    def test_long_path_is_elided_and_fits(self):
        deep = "C:\\Users\\me\\" + "\\".join(f"level{n}folder" for n in range(14))
        row = self.app._format_row(StubResult("report.txt", deep + "\\report.txt"))
        self.assertIn(ui.ELLIPSIS, row)
        self.assertLessEqual(self.row_px(row), self.budget())

    def test_elided_path_keeps_the_tail(self):
        deep = "C:\\a\\" + "\\".join(f"seg{n}" for n in range(30)) + "\\finalfolder"
        row = self.app._format_row(StubResult("x.txt", deep + "\\x.txt"))
        # The folder now runs into the kind column rather than the row's end.
        self.assertIn("finalfolder", row)
        self.assertTrue(row.rstrip().endswith("Text"), row[-20:])

    def test_long_name_is_elided_from_the_right(self):
        name = "extremely" + "long" * 30 + "name.txt"
        row = self.app._format_row(StubResult(name, "C:\\tmp\\" + name))
        self.assertIn(ui.ELLIPSIS, row)
        self.assertTrue(row.lstrip().startswith("extremely"))
        self.assertLessEqual(self.row_px(row), self.budget())

    def test_every_rendered_row_fits_the_window(self):
        long_name = "b" * 120 + ".txt"
        deep = "C:\\" + "\\".join("d" * 12 for _ in range(20))
        rows = [
            StubResult("a.txt", "C:\\a.txt"),
            StubResult(long_name, "C:\\x\\" + long_name),
            StubResult("c.txt", deep + "\\c.txt"),
            StubResult("folder", "C:\\some\\folder", is_dir=True),
        ]
        self.app.controller = StubController(rows)
        self.app.entry.insert(0, "q")
        self.app._run_search()
        self.assertEqual(self.app.row_count(), len(rows))
        for n in range(self.app.row_count()):
            self.assertLessEqual(self.row_px(self.app.row_text(n)),
                                 self.budget(), f"row {n} overflows")

    def test_folder_column_is_aligned_across_rows(self):
        rows = [
            StubResult("a.txt", r"C:\one\a.txt"),
            StubResult("a_much_longer_name.txt", r"C:\two\a_much_longer_name.txt"),
            StubResult("mid.txt", r"C:\three\mid.txt"),
        ]
        starts = []
        for row in self.app._format_rows(rows):
            folder_at = row.index("C:")
            starts.append(self.row_px(row[:folder_at]))
        spread = max(starts) - min(starts)
        self.assertLessEqual(spread, self.row_px("  "),
                             f"folder column ragged by {spread}px")

    def test_icon_key_identifies_folders_and_extensions(self):
        deep = "C:\\" + "\\".join(f"seg{n}" for n in range(30))
        self.assertEqual(self.app._icons.key_for(deep, True), "<dir>")
        self.assertEqual(self.app._icons.key_for("C:\\x\\a.TXT", False), ".txt")

    def test_column_shrinks_when_all_names_are_short(self):
        short = [StubResult("a.txt", r"C:\one.txt"),
                 StubResult("b.txt", r"C:	wo.txt")]
        long = [StubResult("a.txt", r"C:\one.txt"),
                StubResult("a_very_much_longer_filename.txt",
                           r"C:	wo_very_much_longer_filename.txt")]
        short_start = self.app._format_rows(short)[0].index("C:")
        long_start = self.app._format_rows(long)[0].index("C:")
        self.assertLess(self.row_px(self.app._format_rows(short)[0][:short_start]),
                        self.row_px(self.app._format_rows(long)[0][:long_start]),
                        "short names should not reserve a wide column")

    def test_scale_matches_display_dpi(self):
        self.assertGreaterEqual(self.app.scale, 1.0)
        self.assertEqual(self.app.px(10), int(round(10 * self.app.scale)))
        self.assertEqual(self.app.px(-10), -int(round(10 * self.app.scale)))
        self.assertEqual(self.app.px(0), 1, "scaled sizes must never collapse to 0")

    def test_fonts_scale_with_display(self):
        expected = -int(round(abs(ui.ENTRY_PX) * self.app.scale))
        self.assertEqual(self.app.entry_font.cget("size"), expected)

    def test_window_is_dpi_aware(self):
        import ctypes
        awareness = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
        self.assertGreater(awareness.value, 0, "process should not be DPI-virtualised")

    def test_sizing_fits_on_screen(self):
        self.assertLessEqual(self.app.width, self.app.root.winfo_screenwidth())
        self.assertGreater(self.app._row_budget(), 0)
        self.assertGreaterEqual(self.app.rows_visible, 4)
        self.assertLessEqual(self.app.rows_visible, ui.MAX_ROWS)


if __name__ == "__main__":
    unittest.main(verbosity=2)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestPlaceholder(unittest.TestCase):
    """An empty bar should say what it is for."""

    def setUp(self):
        self.app = make(lambda: ui.Launcher(StubController([])))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()

    def type_query(self, text):
        self.app.entry.delete(0, "end")
        self.app.entry.insert(0, text)
        self.app._run_search()

    def test_an_empty_bar_shows_the_hint(self):
        self.assertTrue(self.app._placeholder_up)
        self.assertEqual(self.app.placeholder.cget("text"), ui.PLACEHOLDER)

    def test_typing_takes_it_away(self):
        # It used to stay up and cover what had been typed, because Tk still
        # reported the label as unmapped in the same callback that placed it.
        self.type_query("report")
        self.assertFalse(self.app._placeholder_up)
        self.assertEqual(self.app.placeholder.place_info(), {})

    def test_clearing_brings_it_back(self):
        self.type_query("report")
        self.type_query("")
        self.assertTrue(self.app._placeholder_up)

    def test_it_is_dimmer_than_the_text_it_stands_in_for(self):
        self.assertEqual(self.app.placeholder.cget("fg"), ui.HINT)
        self.assertNotEqual(ui.HINT, ui.FG)

    def test_it_matches_the_entry_it_sits_in(self):
        self.assertEqual(str(self.app.placeholder.cget("font")),
                         str(self.app.entry.cget("font")))

    def test_showing_an_empty_launcher_shows_the_hint(self):
        self.type_query("report")
        self.app.hide()
        self.app.entry.delete(0, "end")
        self.app.show()
        self.assertTrue(self.app._placeholder_up)

    def test_clicking_it_lands_in_the_entry(self):
        self.app.show()
        self.app.placeholder.event_generate("<Button-1>")
        self.app.root.update()
        self.assertEqual(self.app.root.focus_get(), self.app.entry)
