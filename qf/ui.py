"""Borderless Spotlight-style overlay."""
from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from . import shellicon, videopreview
from .usage import OPEN_WEIGHT, REVEAL_WEIGHT

BG = "#1b1c22"
FG = "#f2f3f7"
DIM = "#8b8fa3"
ACCENT = "#5b7cfa"
BORDER = "#3a3d4a"
SEL_BG = "#2e3242"
PANEL_BG = "#202128"

MAX_WIDTH = 720
MIN_WIDTH = 420
MAX_ROWS = 10
DEBOUNCE_MS = 45
CORNER_RADIUS = 16

PREVIEW_WIDTH = 250
PREVIEW_DELAY_MS = 180
PREVIEW_THUMB = 132
ICON_PUMP_MS = 50
# A short silent clip, decoded on the worker and cycled as images.
VIDEO_FRAMES = 16
VIDEO_FPS = 10.0
VIDEO_BOX = 190          # logical px; the pane is 250 wide

# Fonts are given in pixels (negative sizes). Point sizes would be multiplied by
# Tk's own DPI scaling, which is ~1.33 on Windows and made everything oversized.
ENTRY_PX = -20
ROW_PX = -13
STATUS_PX = -11
META_PX = -11
EXCERPT_PX = -10

# Window opacity. Real acrylic/blur is not reachable here: Tk paints an opaque
# client area over the composition layer, and the only workaround -- a colour
# key -- makes those pixels click-through. So this is flat alpha, which dims the
# text along with the panel and washes out over light backgrounds. 0.92 is the
# point where it reads as translucent without losing contrast; `opacity` in
# config.json tunes it.
ALPHA = 0.92
MIN_ALPHA = 0.35
FADE_STEPS = 9
FRAME_MS = 12
SLIDE_PX = 14

SPIN_MS = 60
SPIN_STEP = 30

GA_ROOT = 2
ELLIPSIS = "\u2026"

PROCESS_PER_MONITOR_DPI_AWARE = 2

TEXT_EXTENSIONS = frozenset("""
.txt .md .markdown .rst .log .csv .tsv .json .xml .yaml .yml .toml .ini .cfg
.conf .py .pyw .js .jsx .ts .tsx .html .htm .css .scss .java .c .h .cpp .hpp
.cs .go .rs .rb .php .sh .bash .ps1 .bat .cmd .sql .vbs .r .lua .pl .swift .kt
.pyi .pyx .m .mm .gradle .properties .bazel .cmake .dockerfile .tf .proto
.gitignore .env
""".split())
TEXT_NAMES = frozenset("""
readme license licence copying changelog changes authors contributing notice
todo install news makefile dockerfile procfile .gitignore .gitattributes
.editorconfig .env .npmrc .prettierrc
""".split())
EXCERPT_BYTES = 8192
EXCERPT_LINES = 22


def enable_dpi_awareness() -> None:
    """Opt out of DPI virtualisation before any window exists.

    Without this Windows renders the window at 96 DPI and bitmap-scales it up,
    which on a 200% display means blurry text and geometry that does not match
    the physical screen.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def looks_like_text(raw: bytes) -> bool:
    """Cheap content sniff for files whose name gives nothing away."""
    if not raw:
        return False
    if b"\x00" in raw:
        return False
    printable = sum(1 for byte in raw if byte >= 32 or byte in (9, 10, 13))
    return printable / len(raw) > 0.90


def read_excerpt(path: str) -> str:
    """First few lines of a text-ish file, or '' if it is not one."""
    name = os.path.basename(path).lower()
    extension = os.path.splitext(name)[1]
    known = extension in TEXT_EXTENSIONS or name in TEXT_NAMES
    # README, LICENSE and Makefile carry no extension, so fall back to sniffing
    # the bytes rather than refusing to preview them.
    if not known and extension:
        return ""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(EXCERPT_BYTES)
    except OSError:
        return ""
    if not known and not looks_like_text(raw):
        return ""
    if b"\x00" in raw:
        return ""
    text = raw.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[:EXCERPT_LINES])


def describe(path: str, is_dir: bool, type_name: str) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return type_name or ""
    when = time.strftime("%d %b %Y  %H:%M", time.localtime(info.st_mtime))
    if is_dir:
        return f"{type_name or 'Folder'}\n{when}"
    return f"{type_name or 'File'}\n{human_size(info.st_size)}\n{when}"


class Launcher:
    def __init__(self, controller, opacity=None, preview=True,
                 video_preview=True):
        enable_dpi_awareness()
        self.controller = controller
        self.preview_enabled = bool(preview)
        self.alpha = min(1.0, max(MIN_ALPHA, ALPHA if opacity is None else opacity))
        self.results = []
        self._after_id = None
        self._anim_id = None
        self._spin_id = None
        self._pump_id = None
        self._preview_id = None
        self._anim_offset = 0
        self._spin_angle = 0
        self._busy = False
        self._region_size = None
        self._visible = False

        self._icons = shellicon.IconCache()
        self._icon_images = {}
        self._icon_pending = set()
        self._row_keys = []
        self._jobs: queue.Queue = queue.Queue()
        self._done: queue.Queue = queue.Queue()
        self._worker = None
        self._preview_token = 0
        self._preview_image = None
        self._video_frames = []
        self._video_index = 0
        self._video_id = None
        self.video_enabled = bool(video_preview)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("QuickFind")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg=BORDER)

        self.scale = self._detect_scale()
        self.root.tk.call("tk", "scaling", self.scale * 96.0 / 72.0)
        # 16px shell icons look lost on a 200% display; ask for the 32px set.
        self.large_icons = self.scale >= 1.5

        # Layout constants are authored at 96 DPI and scaled up from there.
        logical_w = self.root.winfo_screenwidth() / self.scale
        logical_h = self.root.winfo_screenheight() / self.scale
        # The preview widens the window rather than squeezing the results, so
        # row text keeps the space it had before the pane existed.
        self.list_width = max(MIN_WIDTH, min(MAX_WIDTH, int(logical_w * 0.55)))
        self.width = self.px(self.list_width
                             + (PREVIEW_WIDTH if self.preview_enabled else 0))
        self.rows_visible = max(4, min(MAX_ROWS, int((logical_h - 260) // 26)))
        self.preview_width = self.px(PREVIEW_WIDTH)

        self.entry_font = tkfont.Font(family="Segoe UI", size=self.px(ENTRY_PX))
        self.row_font = tkfont.Font(family="Segoe UI", size=self.px(ROW_PX))
        status_font = tkfont.Font(family="Segoe UI", size=self.px(STATUS_PX))
        self.meta_font = tkfont.Font(family="Segoe UI", size=self.px(META_PX))
        self.excerpt_font = tkfont.Font(family="Consolas", size=self.px(EXCERPT_PX))

        outer = tk.Frame(self.root, bg=BG, bd=0)
        outer.pack(fill="both", expand=True, padx=self.px(1), pady=self.px(1))

        topbar = tk.Frame(outer, bg=BG)
        topbar.pack(fill="x", padx=self.px(14), pady=(self.px(11), self.px(10)))

        glyph_size = self.px(22)
        self.glyph = tk.Canvas(topbar, width=glyph_size, height=glyph_size, bg=BG,
                               highlightthickness=0, bd=0)
        self.glyph.pack(side="left", padx=(self.px(2), self.px(10)))
        self._draw_magnifier()

        # Packed up front, even while idle, so toggling the spinner never
        # reflows the bar.
        spinner_size = self.px(16)
        self.spinner = tk.Canvas(topbar, width=spinner_size, height=spinner_size,
                                 bg=BG, highlightthickness=0, bd=0)
        self.spinner.pack(side="right", padx=(self.px(8), self.px(2)))

        self.entry = tk.Entry(
            topbar, font=self.entry_font, bg=BG, fg=FG, insertbackground=ACCENT,
            relief="flat", bd=0, highlightthickness=0,
        )
        self.entry.pack(side="left", fill="x", expand=True)

        self.separator = tk.Frame(outer, bg=BORDER, height=1)
        self.body = tk.Frame(outer, bg=BG)

        self._build_tree()
        self._build_preview()

        self.status = tk.Label(outer, font=status_font, bg=BG, fg=DIM, anchor="w")

        self.entry.bind("<KeyRelease>", self._on_key)

        # Bound on the toplevel, not the entry: clicking a row moves focus to
        # the tree, and entry-scoped bindings would stop firing there -- which
        # is why Escape and Enter appeared to stop working after a click.
        self.root.bind("<Escape>", lambda e: (self.hide(), "break")[1])
        self.root.bind("<Return>", self._on_return)
        self.root.bind("<KP_Enter>", self._on_return)
        self.root.bind("<Control-Return>", self._on_reveal)
        self.root.bind("<Control-KP_Enter>", self._on_reveal)
        self.root.bind("<Up>", lambda e: self._move(-1))
        self.root.bind("<Down>", lambda e: self._move(1))
        self.root.bind("<Prior>", lambda e: self._move(-self.rows_visible))
        self.root.bind("<Next>", lambda e: self._move(self.rows_visible))

        self.tree.bind("<Motion>", self._on_hover)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-Button-1>", self._on_return)
        self.root.bind("<FocusOut>", self._on_focus_out)

        self._pump_id = self.root.after(ICON_PUMP_MS, self._pump)

    # -- widgets -----------------------------------------------------------

    def _build_tree(self) -> None:
        style = ttk.Style(self.root)
        # clam honours explicit colours; the native themes ignore most of them.
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "QF.Treeview", background=BG, fieldbackground=BG, foreground=FG,
            borderwidth=0, relief="flat", rowheight=self.px(24),
            font=self.row_font,
        )
        style.map("QF.Treeview",
                  background=[("selected", SEL_BG)],
                  foreground=[("selected", FG)])
        # Drop the border and heading elements entirely.
        style.layout("QF.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        self.tree = ttk.Treeview(self.body, style="QF.Treeview", show="tree",
                                 selectmode="browse", height=self.rows_visible)
        self.tree.column("#0", stretch=True, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)

        self._blank = tk.PhotoImage(width=1, height=1)

    def _build_preview(self) -> None:
        self.preview_rule = tk.Frame(self.body, bg=BORDER, width=1)
        # An explicit height gives the pane a floor, so the excerpt still has
        # room when a search returns only two or three rows.
        self.preview = tk.Frame(self.body, bg=PANEL_BG,
                                width=self.preview_width, height=self.px(250))
        self.preview.pack_propagate(False)

        self.preview_image_label = tk.Label(self.preview, bg=PANEL_BG, bd=0)
        self.preview_image_label.pack(pady=(self.px(14), self.px(8)))

        self.preview_name = tk.Label(
            self.preview, bg=PANEL_BG, fg=FG, font=self.meta_font,
            wraplength=self.preview_width - self.px(24), justify="center")
        self.preview_name.pack(padx=self.px(12))

        self.preview_meta = tk.Label(
            self.preview, bg=PANEL_BG, fg=DIM, font=self.meta_font,
            justify="center")
        self.preview_meta.pack(pady=(self.px(6), self.px(8)), padx=self.px(12))

        self.preview_text = tk.Text(
            self.preview, bg=PANEL_BG, fg=DIM, font=self.excerpt_font,
            relief="flat", bd=0, highlightthickness=0, wrap="none",
            height=8, state="disabled", padx=self.px(10))
        # Packed on demand: an empty excerpt box is just dead space.
        self._excerpt_padding = dict(fill="both", expand=True,
                                     padx=self.px(6), pady=(0, self.px(8)))

    # -- scaling -----------------------------------------------------------

    def _detect_scale(self) -> float:
        try:
            # Scale only when Tk is genuinely working in physical pixels.
            # Two ways it might not be: the process is still DPI-virtualised,
            # or Tk cached its display metrics before awareness applied (it
            # reads them once, at the first Tk() in the process). Either way
            # Windows scales the window itself, and scaling again would
            # double-count and produce a window wider than the screen.
            awareness = ctypes.c_int()
            ctypes.windll.shcore.GetProcessDpiAwareness(
                None, ctypes.byref(awareness))
            if awareness.value == 0:
                return 1.0
            user32 = ctypes.windll.user32
            physical_width = user32.GetSystemMetrics(0)
            if physical_width and self.root.winfo_screenwidth() != physical_width:
                return 1.0
            hwnd = user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
            dpi = user32.GetDpiForWindow(hwnd) or user32.GetDpiForSystem()
            if dpi:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    def px(self, value: int) -> int:
        """Scale a 96-DPI design value to this display, preserving its sign."""
        scaled = int(round(abs(value) * self.scale))
        scaled = max(1, scaled)
        return -scaled if value < 0 else scaled

    # -- chrome ------------------------------------------------------------

    def _draw_magnifier(self) -> None:
        p = self.px
        self.glyph.create_oval(p(3), p(3), p(15), p(15), outline=DIM, width=p(2))
        self.glyph.create_line(p(14), p(14), p(19), p(19),
                               fill=DIM, width=p(2), capstyle="round")

    def _round_corners(self) -> None:
        """Clip the window to a rounded rect.

        DWM's corner preference is ignored for override-redirect windows, so the
        rounding has to be done with an explicit window region instead.
        """
        try:
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            if width <= 1 or height <= 1 or (width, height) == self._region_size:
                return
            hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
            radius = self.px(CORNER_RADIUS)
            region = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, width + 1, height + 1, radius, radius
            )
            # SetWindowRgn takes ownership of the region; it must not be freed.
            ctypes.windll.user32.SetWindowRgn(hwnd, region, True)
            self._region_size = (width, height)
        except Exception:
            pass

    # -- layout ------------------------------------------------------------

    def _position(self) -> None:
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        height = self.root.winfo_reqheight()
        x = (screen_w - self.width) // 2
        y = max(self.px(48), int(screen_h * 0.18)) + self._anim_offset
        self.root.geometry(f"{self.width}x{height}+{x}+{y}")
        self.root.update_idletasks()
        self._round_corners()

    def _sync_panels(self) -> None:
        """Show the results and preview only when they have something to say."""
        if self.results:
            if not self.separator.winfo_ismapped():
                self.separator.pack(fill="x")
                self.body.pack(fill="both", expand=True,
                               padx=self.px(6), pady=(self.px(6), 0))
            self.tree.configure(height=min(self.rows_visible, len(self.results)))
            if self.preview_enabled and not self.preview.winfo_ismapped():
                # Packed right-to-left, so the rule lands between the two.
                self.preview.pack(side="right", fill="y")
                self.preview_rule.pack(side="right", fill="y",
                                       padx=(self.px(6), 0))
        else:
            self.preview.pack_forget()
            self.preview_rule.pack_forget()
            self.body.pack_forget()
            self.separator.pack_forget()

        if self.status.cget("text"):
            if not self.status.winfo_ismapped():
                self.status.pack(fill="x", padx=self.px(16),
                                 pady=(self.px(7), self.px(9)))
        else:
            self.status.pack_forget()
        self._position()

    # -- row text ----------------------------------------------------------

    def _elide(self, text: str, max_px: int, keep_tail: bool) -> str:
        """Trim text to fit, dropping characters from whichever end matters less."""
        font = self.row_font
        if max_px <= 0:
            return ""
        if font.measure(text) <= max_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            candidate = (ELLIPSIS + text[mid:]) if keep_tail else (text[:mid] + ELLIPSIS)
            fits = font.measure(candidate) <= max_px
            if keep_tail:
                if fits:
                    hi = mid
                else:
                    lo = mid + 1
            else:
                if fits:
                    lo = mid + 1
                else:
                    hi = mid
        if keep_tail:
            return ELLIPSIS + text[lo:]
        return text[:max(0, lo - 1)] + ELLIPSIS

    def _row_budget(self) -> int:
        """Width left for row text once the icon and padding take their share."""
        return self.px(self.list_width) - self.px(64)

    def _format_rows(self, results) -> list[str]:
        """Name on the left, folders aligned to a shared column.

        A tree row is one string, so the gap is padded with spaces measured
        against the real font. The column is only as wide as the longest name
        actually present, capped at 45% -- a fixed column would strand short
        names beside a wall of space and leave paths nothing to elide into.
        """
        available = self._row_budget()
        cap = int(available * 0.45)
        measure = self.row_font.measure

        leads = []
        for result in results:
            # No "[dir]" marker any more: the shell icon says what it is.
            leads.append(self._elide(result.name, cap - self.px(12),
                                     keep_tail=False))

        column = min(cap, max((measure(lead) for lead in leads), default=0)
                     + measure("    "))
        space_px = max(1, measure(" "))

        rows = []
        for lead, result in zip(leads, results):
            lead_px = measure(lead)
            pad = max(2, (column - lead_px) // space_px + 1)
            folder = os.path.dirname(result.path) or result.path
            remaining = available - lead_px - pad * space_px
            rows.append(lead + " " * pad + self._elide(folder, remaining,
                                                       keep_tail=True))
        return rows

    def _format_row(self, result) -> str:
        return self._format_rows([result])[0]

    # -- icons and preview, loaded off the UI thread ------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, daemon=True,
                                            name="quickfind-shell")
            self._worker.start()

    def _work(self) -> None:
        """Shell lookups are slow -- a cold icon can take 250ms. Never on the UI."""
        while True:
            job = self._jobs.get()
            if job is None:
                return
            kind = job[0]
            try:
                if kind == "icon":
                    _, key, path, is_dir = job
                    data, _type = self._icons.png_for(path, is_dir,
                                                      self.large_icons)
                    self._done.put(("icon", key, data))
                elif kind == "preview":
                    _, token, path, is_dir = job
                    self._done.put(("preview", token, self._build_preview_data(
                        path, is_dir)))
                elif kind == "video":
                    _, token, path, box = job
                    # Decoding a clip costs about a second. Sweeping down a
                    # list of videos would queue one per row, and the row you
                    # stopped on would wait behind all of them.
                    if token != self._preview_token:
                        continue
                    width, height, pngs = videopreview.frames(
                        path, size=box, count=VIDEO_FRAMES, fps=VIDEO_FPS)
                    if pngs:
                        self._done.put(("video", token, pngs))
            except Exception:
                pass

    def _build_preview_data(self, path: str, is_dir: bool) -> dict:
        image = None
        if not is_dir:
            image = shellicon.thumbnail_png(path, self.px(PREVIEW_THUMB),
                                            thumbnail_only=True)
        is_thumbnail = image is not None
        icon, type_name = self._icons.png_for(path, is_dir, True)
        if image is None:
            image = icon
        return {
            "path": path,
            "image": image,
            "is_thumbnail": is_thumbnail,
            "meta": describe(path, is_dir, type_name),
            "excerpt": "" if is_dir else read_excerpt(path),
            "is_video": (not is_dir) and videopreview.is_video(path),
        }

    def _pump(self) -> None:
        """Drain finished shell work on the UI thread, where Tk is safe."""
        self._pump_id = None
        if not self.alive():
            return
        try:
            while True:
                kind, *payload = self._done.get_nowait()
                if kind == "icon":
                    key, data = payload
                    self._icon_pending.discard(key)
                    if data:
                        try:
                            self._icon_images[key] = tk.PhotoImage(data=data)
                        except tk.TclError:
                            continue
                        self._apply_icon(key)
                elif kind == "preview":
                    token, info = payload
                    if token == self._preview_token:
                        self._show_preview(info)
                elif kind == "video":
                    token, pngs = payload
                    if token == self._preview_token:
                        self._start_video(pngs)
        except queue.Empty:
            pass
        if self.alive():
            self._pump_id = self.root.after(ICON_PUMP_MS, self._pump)

    def _apply_icon(self, key) -> None:
        image = self._icon_images.get(key)
        if image is None:
            return
        items = self.tree.get_children()
        for position, row_key in enumerate(self._row_keys):
            if row_key == key and position < len(items):
                try:
                    self.tree.item(items[position], image=image)
                except tk.TclError:
                    return

    def _request_icon(self, key, path, is_dir) -> None:
        if key in self._icon_images or key in self._icon_pending:
            return
        self._icon_pending.add(key)
        self._ensure_worker()
        self._jobs.put(("icon", key, path, is_dir))

    def _schedule_preview(self) -> None:
        if not self.preview_enabled:
            return
        if self._preview_id is not None:
            try:
                self.root.after_cancel(self._preview_id)
            except Exception:
                pass
        self._preview_id = self.root.after(PREVIEW_DELAY_MS, self._load_preview)

    def _load_preview(self) -> None:
        """Debounced: sweeping the mouse down the list must not hammer the disk."""
        self._preview_id = None
        i = self._selected()
        if i < 0 or i >= len(self.results):
            return
        result = self.results[i]
        self._preview_token += 1
        self.preview_name.configure(text=result.name)
        self._ensure_worker()
        self._jobs.put(("preview", self._preview_token, result.path,
                        result.is_dir))

    def _show_preview(self, info: dict) -> None:
        self._stop_video()
        excerpt = info.get("excerpt", "")
        # A generic page glyph tells you nothing a code excerpt does not; give
        # the space to the text. A real thumbnail earns its place.
        show_image = bool(info.get("image")) and (info.get("is_thumbnail")
                                                  or not excerpt)
        if show_image:
            if not self.preview_image_label.winfo_ismapped():
                # `before` matters: re-packing after pack_forget would
                # otherwise append the image below the text it belongs above.
                self.preview_image_label.pack(before=self.preview_name,
                                              pady=(self.px(14), self.px(8)))
        else:
            self.preview_image_label.pack_forget()

        if show_image:
            try:
                self._preview_image = tk.PhotoImage(data=info["image"])
                self.preview_image_label.configure(image=self._preview_image)
            except tk.TclError:
                self.preview_image_label.configure(image="")
        else:
            self.preview_image_label.configure(image="")
            self._preview_image = None
        self.preview_meta.configure(text=info.get("meta", ""))

        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        if excerpt:
            self.preview_text.insert("1.0", excerpt)
            if not self.preview_text.winfo_ismapped():
                self.preview_text.pack(**self._excerpt_padding)
        else:
            self.preview_text.pack_forget()
        self.preview_text.configure(state="disabled")

        # The still thumbnail is already up; decoding motion takes about a
        # second, so it is requested separately rather than delaying everything.
        if self.video_enabled and info.get("is_video") and show_image:
            box = (self.preview_width - self.px(16), self.px(VIDEO_BOX))
            self._ensure_worker()
            self._jobs.put(("video", self._preview_token, info["path"], box))

    def _start_video(self, pngs) -> None:
        """Swap the still for a looping clip."""
        self._stop_video()
        images = []
        for data in pngs:
            try:
                images.append(tk.PhotoImage(data=data))
            except tk.TclError:
                return
        if not images:
            return
        self._video_frames = images
        self._video_index = 0
        if not self.preview_image_label.winfo_ismapped():
            self.preview_image_label.pack(before=self.preview_name,
                                          pady=(self.px(14), self.px(8)))
        self._advance_video()

    def _advance_video(self) -> None:
        self._video_id = None
        if not self._video_frames or not self.alive():
            return
        frame = self._video_frames[self._video_index % len(self._video_frames)]
        self._video_index += 1
        try:
            self.preview_image_label.configure(image=frame)
        except tk.TclError:
            return
        self._video_id = self.root.after(int(1000 / VIDEO_FPS),
                                         self._advance_video)

    def _stop_video(self) -> None:
        if self._video_id is not None:
            try:
                self.root.after_cancel(self._video_id)
            except Exception:
                pass
            self._video_id = None
        # Dropping the PhotoImages matters: sixteen frames of a pane-width clip
        # is several megabytes inside Tk.
        self._video_frames = []
        self._video_index = 0

    def _clear_preview(self) -> None:
        self._stop_video()
        self._preview_token += 1
        self._preview_image = None
        self.preview_image_label.configure(image="")
        self.preview_name.configure(text="")
        self.preview_meta.configure(text="")
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.configure(state="disabled")
        self.preview_text.pack_forget()

    # -- animation ---------------------------------------------------------

    def _cancel_anim(self) -> None:
        if self._anim_id is not None:
            try:
                self.root.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _fade_in(self) -> None:
        self._cancel_anim()

        def frame(step):
            self._anim_id = None
            if not self.alive() or not self._visible:
                return
            progress = step / FADE_STEPS
            eased = 1 - (1 - progress) ** 3
            self.root.attributes("-alpha", self.alpha * eased)
            self._anim_offset = int(self.px(SLIDE_PX) * (1 - eased))
            self._position()
            if step < FADE_STEPS:
                self._anim_id = self.root.after(FRAME_MS, frame, step + 1)
            else:
                self._anim_offset = 0

        frame(1)

    def _spin(self) -> None:
        if self._spin_id is not None:
            try:
                self.root.after_cancel(self._spin_id)
            except Exception:
                pass
            self._spin_id = None
        if not (self._busy and self._visible and self.alive()):
            return
        self._spin_angle = (self._spin_angle - SPIN_STEP) % 360
        self.spinner.delete("all")
        p = self.px
        self.spinner.create_arc(p(2), p(2), p(14), p(14),
                                start=self._spin_angle, extent=100,
                                style="arc", outline=ACCENT, width=p(2))
        self._spin_id = self.root.after(SPIN_MS, self._spin)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._spin()
        else:
            if self._spin_id is not None:
                try:
                    self.root.after_cancel(self._spin_id)
                except Exception:
                    pass
                self._spin_id = None
            if self.alive():
                self.spinner.delete("all")

    # -- visibility --------------------------------------------------------

    def show(self) -> None:
        if self._visible:
            self.entry.select_range(0, "end")
            self._focus()
            return
        self._visible = True
        self._anim_offset = self.px(SLIDE_PX)
        self.root.attributes("-alpha", 0.0)
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.entry.select_range(0, "end")
        self._sync_panels()
        self._focus()
        self._fade_in()
        if self._busy:
            self._spin()

    def hide(self) -> None:
        self._visible = False
        self._cancel_anim()
        for attr in ("_after_id", "_preview_id"):
            pending = getattr(self, attr)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._stop_video()
        self._anim_offset = 0
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def is_visible(self) -> bool:
        return self._visible

    def shutdown(self) -> None:
        """Drop every pending callback so Tk is not left holding a dead one."""
        self._visible = False
        self._busy = False
        self._cancel_anim()
        self._stop_video()
        for attr in ("_after_id", "_spin_id", "_pump_id", "_preview_id"):
            pending = getattr(self, attr)
            if pending is not None:
                try:
                    self.root.after_cancel(pending)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._jobs.put(None)

    def alive(self) -> bool:
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _focus(self) -> None:
        self.entry.focus_force()
        try:
            hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _on_focus_out(self, _event) -> None:
        if self._visible and self.root.focus_get() is None:
            self.hide()

    # -- searching ---------------------------------------------------------

    def _on_key(self, event) -> None:
        if event.keysym in ("Up", "Down", "Return", "Escape", "Prior", "Next"):
            return
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(DEBOUNCE_MS, self._run_search)

    def row_count(self) -> int:
        return len(self.tree.get_children())

    def row_text(self, index: int) -> str:
        items = self.tree.get_children()
        return self.tree.item(items[index], "text")

    def _run_search(self) -> None:
        self._after_id = None
        query = self.entry.get()
        self.results, note = self.controller.query(query)

        # Reuse rows rather than clearing and rebuilding: recreating 40 tree
        # items on every keystroke costs far more than reconfiguring them.
        items = list(self.tree.get_children())
        self.tree.selection_remove(*items)
        self._row_keys = []
        texts = self._format_rows(self.results)

        for position, (result, text) in enumerate(zip(self.results, texts)):
            key = self._icons.key_for(result.path, result.is_dir)
            self._row_keys.append(key)
            image = self._icon_images.get(key, self._blank)
            if position < len(items):
                self.tree.item(items[position], text=text, image=image)
            else:
                self.tree.insert("", "end", text=text, image=image)
            if key not in self._icon_images:
                self._request_icon(key, result.path, result.is_dir)

        for surplus in items[len(texts):]:
            self.tree.delete(surplus)

        if self.results:
            self._select(0)
        else:
            self._clear_preview()
        self.status.configure(text=note)
        self._sync_panels()

    def set_status(self, text: str) -> None:
        if not self.alive():
            return
        self.status.configure(text=text)
        if self._visible:
            self._sync_panels()

    # -- interaction -------------------------------------------------------

    def _selected(self) -> int:
        chosen = self.tree.selection()
        if not chosen:
            return -1
        try:
            return self.tree.index(chosen[0])
        except tk.TclError:
            return -1

    def _select(self, target: int) -> None:
        if not self.results:
            return
        items = self.tree.get_children()
        if not items:
            return
        target = max(0, min(len(items) - 1, target))
        if self._selected() == target:
            return
        self.tree.selection_set(items[target])
        self.tree.focus(items[target])
        self._schedule_preview()

    def _move(self, delta: int) -> str:
        if not self.results:
            return "break"
        current = self._selected()
        self._select((current if current >= 0 else 0) + delta)
        chosen = self.tree.selection()
        if chosen:
            self.tree.see(chosen[0])
        return "break"

    def _row_at(self, y: int) -> int:
        item = self.tree.identify_row(y)
        if not item:
            return -1
        try:
            return self.tree.index(item)
        except tk.TclError:
            return -1

    def _on_hover(self, event) -> None:
        target = self._row_at(event.y)
        if target >= 0:
            self._select(target)

    def _on_click(self, event) -> str:
        target = self._row_at(event.y)
        if target >= 0:
            self._select(target)
        # Keep the caret in the entry so typing still refines the search.
        self.entry.focus_set()
        return "break"

    def _activate(self, opener, weight=1.0) -> str:
        i = self._selected()
        if i < 0:
            return "break"
        target = self.results[i].path
        if not os.path.exists(target):
            # The index is a snapshot; say so rather than failing silently.
            self.set_status(f"No longer on disk: {target}")
            return "break"
        self.hide()
        if not opener(target):
            self.set_status(f"Could not open: {target}")
            return "break"
        record = getattr(self.controller, "record_use", None)
        if record is not None:
            record(target, weight)
        return "break"

    def _on_return(self, _event) -> str:
        query = self.entry.get().strip()
        if query.startswith(":"):
            self.controller.command(query)
            return "break"
        return self._activate(open_path, OPEN_WEIGHT)

    def _on_reveal(self, _event) -> str:
        # Revealing is a weaker signal of intent than opening.
        return self._activate(reveal_path, REVEAL_WEIGHT)

    def run(self) -> None:
        self.root.mainloop()


def open_path(path: str) -> bool:
    """Open a file with its default handler, or a folder in Explorer."""
    try:
        os.startfile(os.path.normpath(path))
        return True
    except OSError:
        return reveal_path(path)


def reveal_path(path: str) -> bool:
    """Open the containing folder with the item highlighted.

    Enter opens a folder; Ctrl+Enter shows it where it lives, so /select is
    right for files and folders alike.
    """
    target = os.path.normpath(path)
    try:
        # Passed as one string so CreateProcess gets `/select,"C:\with space"`.
        # The list form would quote the whole argument instead, which Explorer
        # mis-parses -- it opens Documents rather than selecting the item.
        # Windows filenames cannot contain a quote, so this cannot be escaped.
        subprocess.Popen(f'explorer /select,"{target}"')
        return True
    except OSError:
        return False
