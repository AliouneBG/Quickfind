"""Shell icons, thumbnails and the preview pane."""
import gc
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import tkinter as tk
except Exception:
    tk = None
from _tkcheck import TK_AVAILABLE, make

from qf import shellicon, ui


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

    def query(self, text):
        return (self.rows, "ok") if text.strip() else ([], "")

    def command(self, text):
        pass


class TestPngEncoder(unittest.TestCase):
    def test_signature_and_dimensions(self):
        data = shellicon.encode_png(3, 2, bytes(3 * 2 * 4))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
        self.assertEqual((width, height, depth, colour), (3, 2, 8, 6))

    def test_ends_with_iend(self):
        self.assertTrue(shellicon.encode_png(1, 1, bytes(4)).endswith(b"IEND\xae\x42\x60\x82"))

    def test_round_trips_through_tk(self):
        if not TK_AVAILABLE:
            self.skipTest("no Tk")
        root = make(tk.Tk)
        root.withdraw()
        try:
            pixels = bytes([255, 0, 0, 255] * 4)
            image = tk.PhotoImage(data=shellicon.encode_png(2, 2, pixels))
            self.assertEqual((image.width(), image.height()), (2, 2))
        finally:
            root.destroy()
            gc.collect()


class TestIconCache(unittest.TestCase):
    def setUp(self):
        self.cache = shellicon.IconCache()

    def test_folders_share_one_key(self):
        self.assertEqual(self.cache.key_for(r"C:\a", True), "<dir>")
        self.assertEqual(self.cache.key_for(r"C:\b\c", True), "<dir>")

    def test_extension_is_the_key_and_is_lowercased(self):
        self.assertEqual(self.cache.key_for(r"C:\a\b.TXT", False), ".txt")
        self.assertEqual(self.cache.key_for(r"C:\other\c.txt", False), ".txt")

    def test_executables_key_per_file(self):
        # An .exe carries its own icon, so it cannot share one by extension.
        key = self.cache.key_for(r"C:\Windows\notepad.exe", False)
        self.assertNotEqual(key, ".exe")
        self.assertIn("notepad.exe", key)

    def test_extensionless_files_have_a_key(self):
        self.assertEqual(self.cache.key_for(r"C:\a\LICENSE", False), "<none>")

    def test_real_lookup_returns_a_png_and_type_name(self):
        data, type_name = self.cache.png_for(r"C:\x\a.txt", False)
        self.assertTrue(data and data[:8] == b"\x89PNG\r\n\x1a\n")
        self.assertTrue(type_name)

    def test_second_lookup_is_cached(self):
        first = self.cache.png_for(r"C:\x\b.txt", False)
        second = self.cache.png_for(r"C:\y\c.txt", False)
        self.assertIs(first[0], second[0], "same extension must reuse the icon")


class TestPreviewHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data, mode="w"):
        path = os.path.join(self.tmp.name, name)
        with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
            fh.write(data)
        return path

    def test_human_size(self):
        self.assertEqual(ui.human_size(0), "0 B")
        self.assertEqual(ui.human_size(512), "512 B")
        self.assertEqual(ui.human_size(2048), "2.0 KB")
        self.assertIn("MB", ui.human_size(5 * 1024 * 1024))

    def test_excerpt_of_a_text_file(self):
        path = self.write("a.txt", "line one\nline two\nline three\n")
        self.assertIn("line one", ui.read_excerpt(path))

    def test_excerpt_is_capped(self):
        path = self.write("big.txt", "\n".join(str(n) for n in range(500)))
        self.assertLessEqual(len(ui.read_excerpt(path).splitlines()),
                             ui.EXCERPT_LINES)

    def test_no_excerpt_for_unknown_extension(self):
        path = self.write("a.bin", "text but wrong extension")
        self.assertEqual(ui.read_excerpt(path), "")

    def test_no_excerpt_for_binary_content(self):
        path = self.write("a.txt", b"abc\x00\x01binary", mode="wb")
        self.assertEqual(ui.read_excerpt(path), "")

    def test_excerpt_survives_bad_encoding(self):
        path = self.write("a.txt", b"caf\xe9 not utf8", mode="wb")
        self.assertIn("caf", ui.read_excerpt(path))

    def test_missing_file_has_no_excerpt(self):
        self.assertEqual(ui.read_excerpt(os.path.join(self.tmp.name, "nope.txt")), "")

    def test_excerpt_of_an_extensionless_readme(self):
        path = self.write("README", "QuickFind\n=========\n")
        self.assertIn("QuickFind", ui.read_excerpt(path))

    def test_excerpt_of_a_makefile(self):
        path = self.write("Makefile", "all:\n\tpython -m unittest\n")
        self.assertIn("unittest", ui.read_excerpt(path))

    def test_extensionless_binary_is_sniffed_out(self):
        path = self.write("blob", bytes(range(256)) * 8, mode="wb")
        self.assertEqual(ui.read_excerpt(path), "")

    def test_unknown_extension_is_not_sniffed(self):
        # A known-binary extension should not cost a read at all.
        path = self.write("a.dll", "actually text")
        self.assertEqual(ui.read_excerpt(path), "")

    def test_looks_like_text(self):
        self.assertTrue(ui.looks_like_text(b"plain ascii\n"))
        self.assertFalse(ui.looks_like_text(b"\x00\x01\x02"))
        self.assertFalse(ui.looks_like_text(b""))

    def test_describe_a_file(self):
        path = self.write("a.txt", "hello")
        text = ui.describe(path, False, "Text Document")
        self.assertIn("Text Document", text)
        self.assertIn("5 B", text)

    def test_describe_a_folder_omits_size(self):
        text = ui.describe(self.tmp.name, True, "File folder")
        self.assertIn("File folder", text)
        self.assertNotIn(" B\n", text)

    def test_describe_a_missing_path(self):
        self.assertEqual(ui.describe(r"C:\nope\nope.txt", False, "File"), "File")


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestIconPipeline(unittest.TestCase):
    def setUp(self):
        self.rows = [Row("a.txt", r"C:\x\a.txt"), Row("b.txt", r"C:\y\b.txt"),
                     Row("docs", r"C:\docs", is_dir=True)]
        self.app = make(lambda: ui.Launcher(Stub(self.rows)))
        self.app._ensure_worker = lambda: None      # no real shell calls
        self.app.entry.insert(0, "q")
        self.app._run_search()

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def test_rows_render_before_icons_arrive(self):
        self.assertEqual(self.app.row_count(), 3)

    def test_one_request_per_distinct_key(self):
        # Two .txt rows share an extension, so only two jobs: .txt and <dir>.
        keys = {job[1] for job in list(self.app._jobs.queue) if job[0] == "icon"}
        self.assertEqual(keys, {".txt", "<dir>"})

    def test_repeat_requests_are_suppressed(self):
        before = self.app._jobs.qsize()
        self.app._request_icon(".txt", r"C:\x\a.txt", False)
        self.assertEqual(self.app._jobs.qsize(), before)

    def test_pump_applies_an_icon_to_matching_rows(self):
        png = shellicon.encode_png(2, 2, bytes([90, 90, 90, 255] * 4))
        self.app._done.put(("icon", ".txt", png))
        self.app._pump()
        self.assertIn(".txt", self.app._icon_images)
        items = self.app.tree.get_children()
        self.assertTrue(self.app.tree.item(items[0], "image"))
        self.assertTrue(self.app.tree.item(items[1], "image"))

    def test_pump_survives_undecodable_image_data(self):
        self.app._done.put(("icon", ".txt", b"not a png"))
        self.app._pump()
        self.assertNotIn(".txt", self.app._icon_images)

    def test_failed_lookup_clears_pending(self):
        self.app._done.put(("icon", "<dir>", None))
        self.app._pump()
        self.assertNotIn("<dir>", self.app._icon_pending)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestPreviewPane(unittest.TestCase):
    def setUp(self):
        self.rows = [Row("a.txt", r"C:\x\a.txt"), Row("b.txt", r"C:\y\b.txt")]
        self.app = make(lambda: ui.Launcher(Stub(self.rows)))
        self.app._ensure_worker = lambda: None
        self.app.entry.insert(0, "q")
        self.app._run_search()

    def tearDown(self):
        self.app.shutdown()
        self.app.root.destroy()
        self.app = None
        gc.collect()

    def test_selecting_schedules_a_preview(self):
        self.app._select(1)
        self.assertIsNotNone(self.app._preview_id)

    def test_scheduling_twice_leaves_one_pending(self):
        self.app._schedule_preview()
        first = self.app._preview_id
        self.app._schedule_preview()
        self.assertNotEqual(self.app._preview_id, first,
                            "the earlier timer must be replaced")

    def test_load_requests_the_selected_row(self):
        self.app._select(1)
        self.app._load_preview()
        jobs = [j for j in list(self.app._jobs.queue) if j[0] == "preview"]
        self.assertTrue(jobs)
        self.assertEqual(jobs[-1][2], r"C:\y\b.txt")

    def test_stale_results_are_ignored(self):
        self.app._load_preview()
        stale = self.app._preview_token
        self.app._load_preview()          # token advances
        self.app._done.put(("preview", stale, {"meta": "STALE", "excerpt": ""}))
        self.app._pump()
        self.assertNotIn("STALE", self.app.preview_meta.cget("text"))

    def test_current_results_are_shown(self):
        self.app._load_preview()
        info = {"image": None, "meta": "Text Document\n5 B", "excerpt": "hello"}
        self.app._done.put(("preview", self.app._preview_token, info))
        self.app._pump()
        self.assertIn("Text Document", self.app.preview_meta.cget("text"))
        self.assertIn("hello", self.app.preview_text.get("1.0", "end"))

    def test_excerpt_box_is_hidden_without_an_excerpt(self):
        self.app.show()
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": None, "meta": "Application", "excerpt": ""}))
        self.app._pump()
        self.app.root.update()
        self.assertFalse(self.app.preview_text.winfo_ismapped(),
                         "an empty excerpt box is just dead space")

    def test_excerpt_box_appears_when_there_is_one(self):
        self.app.show()
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": None, "meta": "Text", "excerpt": "hello"}))
        self.app._pump()
        self.app.root.update()
        self.assertTrue(self.app.preview_text.winfo_ismapped())

    def test_code_excerpt_replaces_a_generic_icon(self):
        self.app.show()   # children are not mapped while the root is withdrawn
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": b"x", "is_thumbnail": False,
                             "meta": "Python.File", "excerpt": "import os"}))
        self.app._pump()
        self.app.root.update()
        self.assertFalse(self.app.preview_image_label.winfo_ismapped(),
                         "a blank page glyph should yield to the code")

    def test_real_thumbnail_is_kept_alongside_an_excerpt(self):
        self.app.show()
        png = shellicon.encode_png(2, 2, bytes([10, 20, 30, 255] * 4))
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": png, "is_thumbnail": True,
                             "meta": "PNG", "excerpt": "not really text"}))
        self.app._pump()
        self.app.root.update()
        self.assertTrue(self.app.preview_image_label.winfo_ismapped())

    def test_image_returns_above_the_name_after_being_hidden(self):
        png = shellicon.encode_png(2, 2, bytes([10, 20, 30, 255] * 4))
        first = self.app.preview.pack_slaves()[0]
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": b"x", "is_thumbnail": False,
                             "meta": "m", "excerpt": "code"}))
        self.app._pump()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": png, "is_thumbnail": True,
                             "meta": "m", "excerpt": ""}))
        self.app._pump()
        self.assertIs(self.app.preview.pack_slaves()[0], first,
                      "re-packing must restore the original position")

    def test_excerpt_is_read_only(self):
        self.assertEqual(str(self.app.preview_text.cget("state")), "disabled")

    def test_clearing_empties_the_pane(self):
        self.app._load_preview()
        self.app._done.put(("preview", self.app._preview_token,
                            {"image": None, "meta": "x", "excerpt": "y"}))
        self.app._pump()
        self.app._clear_preview()
        self.assertEqual(self.app.preview_meta.cget("text"), "")
        self.assertEqual(self.app.preview_text.get("1.0", "end").strip(), "")

    def test_empty_results_hide_the_pane(self):
        self.app.show()
        self.app.entry.delete(0, "end")
        self.app._run_search()
        self.assertFalse(self.app.preview.winfo_ismapped())

    def test_pane_is_visible_with_results(self):
        self.app.show()
        self.app.root.update()
        self.assertTrue(self.app.preview.winfo_ismapped())


if __name__ == "__main__":
    unittest.main(verbosity=2)
