"""Rendering the first page of a PDF with the renderer Windows ships.

The fixtures are PDFs this file writes by hand, so the suite does not depend
on any document happening to exist on the machine.
"""
import gc
import os
import struct
import sys
import tempfile
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

from qf import pdfpreview, ui


def write_pdf(path, text="HELLO QUICKFIND", width=300, height=120, pages=1):
    """A minimal but genuinely valid PDF, offsets and all."""
    content = "BT /F1 24 Tf 20 60 Td ({}) Tj ET\n".format(text).encode("ascii")
    kids = " ".join("{} 0 R".format(3 + n) for n in range(pages))
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        "<</Type/Pages/Kids[{}]/Count {}>>".format(kids, pages).encode(),
    ]
    for _ in range(pages):
        objects.append(
            ("<</Type/Page/Parent 2 0 R/MediaBox[0 0 {} {}]"
             "/Contents {} 0 R/Resources<</Font<</F1 {} 0 R>>>>>>").format(
                 width, height, 3 + pages, 4 + pages).encode())
    objects.append(b"<</Length " + str(len(content)).encode() + b">>\nstream\n"
                   + content + b"endstream")
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(n).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref_at = len(out)
    out += "xref\n0 {}\n".format(len(objects) + 1).encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += "{:010d} 00000 n \n".format(offset).encode()
    out += ("trailer\n<</Size {}/Root 1 0 R>>\nstartxref\n{}\n%%EOF\n"
            .format(len(objects) + 1, xref_at)).encode()
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return path


def size_of(png):
    return struct.unpack(">II", png[16:24])


class TestIsPdf(unittest.TestCase):
    def test_by_extension(self):
        self.assertTrue(pdfpreview.is_pdf("report.pdf"))
        self.assertTrue(pdfpreview.is_pdf(r"C:\x\REPORT.PDF"))

    def test_everything_else(self):
        for name in ("a.txt", "b.docx", "c.pdfx", "d"):
            self.assertFalse(pdfpreview.is_pdf(name), name)


class TestFit(unittest.TestCase):
    def page(self, width, height):
        return pdfpreview.Size(float(width), float(height))

    def test_a_portrait_page_is_fitted_by_height(self):
        # A4 is taller than it is wide, so the height is what runs out first.
        self.assertLess(pdfpreview._fit(self.page(595, 842), (400, 400)), 400)

    def test_a_landscape_page_is_fitted_by_width(self):
        self.assertEqual(pdfpreview._fit(self.page(800, 200), (400, 400)), 400)

    def test_a_nonsense_page_falls_back_to_the_box(self):
        self.assertEqual(pdfpreview._fit(self.page(0, 0), (320, 240)), 320)

    def test_never_asks_for_nothing(self):
        self.assertGreaterEqual(pdfpreview._fit(self.page(595, 842), (1, 1)), 8)


class TestRendering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = write_pdf(os.path.join(self.tmp.name, "sample.pdf"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_page_comes_back_as_a_png(self):
        png, pages = pdfpreview.first_page(self.pdf)
        self.assertIsNotNone(png, "nothing was rendered")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(pages, 1)

    def test_the_page_fits_the_box_it_was_given(self):
        for box in ((440, 340), (200, 200), (900, 200)):
            png, _pages = pdfpreview.first_page(self.pdf, box=box)
            self.assertIsNotNone(png, box)
            width, height = size_of(png)
            self.assertLessEqual(width, box[0], box)
            self.assertLessEqual(height, box[1], box)

    def test_the_page_fills_the_box_it_was_given(self):
        # Fitting is not enough: a page rendered at a quarter of the pane
        # would be a postage stamp in a lot of white space.
        box = (440, 340)
        png, _pages = pdfpreview.first_page(self.pdf, box=box)
        width, height = size_of(png)
        self.assertTrue(width >= box[0] * 0.9 or height >= box[1] * 0.9,
                        "rendered {}x{} into {}".format(width, height, box))

    def test_the_proportions_of_the_page_are_kept(self):
        png, _pages = pdfpreview.first_page(self.pdf, box=(440, 340))
        width, height = size_of(png)
        self.assertAlmostEqual(width / height, 300 / 120, places=1)

    def test_the_page_count_is_reported(self):
        many = write_pdf(os.path.join(self.tmp.name, "many.pdf"), pages=3)
        _png, pages = pdfpreview.first_page(many)
        self.assertEqual(pages, 3)

    def test_rendering_twice_gives_the_same_size(self):
        first, _ = pdfpreview.first_page(self.pdf, box=(440, 340))
        second, _ = pdfpreview.first_page(self.pdf, box=(440, 340))
        self.assertEqual(size_of(first), size_of(second))

    def test_a_missing_file_renders_nothing(self):
        self.assertEqual(
            pdfpreview.first_page(os.path.join(self.tmp.name, "no.pdf")),
            (None, 0))

    def test_something_that_is_not_a_pdf_renders_nothing(self):
        fake = os.path.join(self.tmp.name, "fake.pdf")
        with open(fake, "wb") as fh:
            fh.write(b"this is not a pdf at all")
        self.assertEqual(pdfpreview.first_page(fake), (None, 0))

    def test_a_truncated_pdf_renders_nothing(self):
        broken = os.path.join(self.tmp.name, "broken.pdf")
        with open(self.pdf, "rb") as fh:
            head = fh.read(120)
        with open(broken, "wb") as fh:
            fh.write(head)
        self.assertEqual(pdfpreview.first_page(broken), (None, 0))

    def test_an_empty_file_renders_nothing(self):
        empty = os.path.join(self.tmp.name, "empty.pdf")
        open(empty, "wb").close()
        self.assertEqual(pdfpreview.first_page(empty), (None, 0))

    def test_a_relative_path_still_works(self):
        # WinRT rejects relative paths outright, so they are made absolute.
        here = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            png, _pages = pdfpreview.first_page("sample.pdf")
            self.assertIsNotNone(png)
        finally:
            os.chdir(here)

    def test_nothing_is_left_behind_in_temp(self):
        before = _leftovers()
        for _ in range(3):
            pdfpreview.first_page(self.pdf)
        self.assertEqual(_leftovers(), before)

    def test_rendering_is_quick_enough_to_run_on_a_hover(self):
        pdfpreview.first_page(self.pdf)          # pay any one-off cost first
        start = time.perf_counter()
        pdfpreview.first_page(self.pdf)
        self.assertLess((time.perf_counter() - start) * 1000, 800)


def _leftovers():
    return len([name for name in os.listdir(tempfile.gettempdir())
                if name.startswith("quickfind-pdf-")])


class TestMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = write_pdf(os.path.join(self.tmp.name, "sample.pdf"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_page_count_is_shown(self):
        meta = ui.describe(self.pdf, False, "PDF Document", pages=7)
        self.assertIn("7 pages", meta)

    def test_one_page_is_singular(self):
        meta = ui.describe(self.pdf, False, "PDF Document", pages=1)
        self.assertIn("1 page", meta)
        self.assertNotIn("1 pages", meta)

    def test_nothing_is_said_when_there_is_no_count(self):
        meta = ui.describe(self.pdf, False, "PDF Document")
        self.assertNotIn("page", meta)


@unittest.skipUnless(TK_AVAILABLE, "no Tk display available")
class TestPreviewPane(unittest.TestCase):
    class Stub:
        def query(self, text):
            return ([], "")

        def command(self, text):
            pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = write_pdf(os.path.join(self.tmp.name, "sample.pdf"))
        self.app = make(lambda: ui.Launcher(self.Stub()))

    def tearDown(self):
        try:
            self.app.shutdown()
            self.app.root.destroy()
        except Exception:
            pass
        self.app = None
        gc.collect()
        self.tmp.cleanup()

    def test_a_pdf_gets_a_rendered_page(self):
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None):
            info = self.app._build_preview_data(self.pdf, False)
        self.assertTrue(info["is_thumbnail"], "the page was not used")
        self.assertEqual(info["image"][:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("1 page", info["meta"])

    def test_the_shell_is_preferred_when_it_has_a_thumbnail(self):
        # A machine with Acrobat has a handler, and its thumbnail is the one
        # Explorer shows, so there is no reason to render the page again.
        with mock.patch.object(ui.shellicon, "thumbnail_png",
                               return_value=b"shell-thumbnail"), \
             mock.patch.object(ui.pdfpreview, "first_page") as render:
            info = self.app._build_preview_data(self.pdf, False)
        render.assert_not_called()
        self.assertEqual(info["image"], b"shell-thumbnail")

    def test_a_cloud_only_pdf_is_not_rendered(self):
        # Rendering one would ask OneDrive to download the whole file.
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None), \
             mock.patch.object(ui, "is_cloud_only", return_value=True), \
             mock.patch.object(ui.pdfpreview, "first_page") as render:
            info = self.app._build_preview_data(self.pdf, False)
        render.assert_not_called()
        self.assertIn("Online only", info["meta"])

    def test_other_files_are_not_sent_to_the_renderer(self):
        other = os.path.join(self.tmp.name, "notes.txt")
        with open(other, "w") as fh:
            fh.write("hello")
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None), \
             mock.patch.object(ui.pdfpreview, "first_page") as render:
            self.app._build_preview_data(other, False)
        render.assert_not_called()

    def test_a_pdf_that_will_not_render_falls_back_to_the_icon(self):
        with mock.patch.object(ui.shellicon, "thumbnail_png", return_value=None), \
             mock.patch.object(ui.pdfpreview, "first_page",
                               return_value=(None, 0)):
            info = self.app._build_preview_data(self.pdf, False)
        self.assertFalse(info["is_thumbnail"])
        self.assertIsNotNone(info["image"], "no icon to fall back on")


if __name__ == "__main__":
    unittest.main(verbosity=2)
