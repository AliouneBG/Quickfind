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
from ctypes import wintypes
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

SPI_GETWHEELSCROLLLINES = 0x0068
WHEEL_PAGESCROLL = 0xFFFFFFFF
DEFAULT_WHEEL_LINES = 3
MAX_WHEEL_LINES = 24
# The list is a viewport: only the rows on screen exist as widgets, however
# many results there are. The folder column is measured once per search from
# at most this many rows, so that it does not shift about while scrolling.
COLUMN_SAMPLE_ROWS = 200

# How long after a keystroke the rest of the matches are fetched. Long enough
# that a burst of typing never pays for it, short enough to be ready well
# before anyone reaches for the wheel.
DEEPEN_MS = 120

PREVIEW_WIDTH = 250
PREVIEW_DELAY_MS = 180
PREVIEW_THUMB = 132
ICON_PUMP_MS = 50
# A short silent clip, decoded on the worker and cycled as images.
# Six runs of eighteen frames, sampled across the clip: five and a half seconds
# of playback showing six different moments rather than a second and a half of
# one. 20fps is what the pane can actually paint steadily; see _next_delay.
VIDEO_SEGMENTS = 6
VIDEO_PER_SEGMENT = 18
VIDEO_FPS = 20.0
# Enough of the first run to start on, then one per frame after that. Turning a
# PNG into a Tk image costs about 10ms, so converting a whole run at once
# stalled the animation for a fifth of a second every time one arrived. One per
# tick is 20 a second, which still outruns the decoder, and it measured
# steadier than two (3.0ms of jitter against 6.7ms).
VIDEO_OPENING_FRAMES = 4
VIDEO_CONVERT_PER_TICK = 1
# Runs arrive about a second apart and each is 0.9s of playback, so starting on
# the first one means the loop reaches its end and shows the opening frame
# again before there is anything new to show. Holding the still thumbnail until
# a second run is in hand leaves 0.65s of slack, and the preview then never
# repeats until the whole clip is decoded, where looping is the point.
VIDEO_BUFFER_RUNS = 2
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


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetFileAttributesW.restype = wintypes.DWORD
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
CLOUD_ONLY = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN
              | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def wheel_lines() -> int:
    """Rows one wheel notch should scroll, as Windows is configured.

    Tk's own Treeview binding always scrolls exactly one row, which on a ten
    row list feels like wading. Windows' default is three, and it is a setting
    people genuinely change, so ask rather than assume.
    """
    value = ctypes.c_uint(DEFAULT_WHEEL_LINES)
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWHEELSCROLLLINES, 0, ctypes.byref(value), 0)
    except Exception:
        return DEFAULT_WHEEL_LINES
    if not ok:
        return DEFAULT_WHEEL_LINES
    if value.value == WHEEL_PAGESCROLL:
        return MAX_ROWS
    return max(1, min(int(value.value), MAX_WHEEL_LINES))


def _fine_timer(wanted: bool) -> bool:
    """Ask Windows for 1ms timers, or give them back.

    The default scheduling tick is 15.6ms, which every `after` delay is rounded
    up to. That alone held a nominal 12fps animation to 10.7fps with 6ms of
    jitter; with 1ms ticks the same loop holds 20fps to within half a
    millisecond. Returns whether the request was honoured.
    """
    try:
        winmm = ctypes.WinDLL("winmm")
        call = winmm.timeBeginPeriod if wanted else winmm.timeEndPeriod
        return call(1) == 0
    except Exception:
        return False


def file_attributes(path: str) -> int:
    try:
        return _kernel32.GetFileAttributesW(path)
    except Exception:
        return INVALID_FILE_ATTRIBUTES


def is_cloud_only(path: str) -> bool:
    """A file whose contents live in the cloud rather than on this machine.

    OneDrive leaves a placeholder with these attributes. Reading one asks
    OneDrive to fetch the whole file, which is not something hovering a row
    should start, and when the fetch cannot happen the read fails outright.
    """
    attributes = file_attributes(path)
    if attributes == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attributes & CLOUD_ONLY)


def read_excerpt(path: str) -> str:
    """First few lines of a text-ish file, or '' if it is not one."""
    name = os.path.basename(path).lower()
    extension = os.path.splitext(name)[1]
    known = extension in TEXT_EXTENSIONS or name in TEXT_NAMES
    # README, LICENSE and Makefile carry no extension, so fall back to sniffing
    # the bytes rather than refusing to preview them.
    if not known and extension:
        return ""
    if is_cloud_only(path):
        # Opening it would ask OneDrive to download the whole file.
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


def describe(path: str, is_dir: bool, type_name: str,
             cloud_only: bool = False) -> str:
    try:
        info = os.stat(path)
    except OSError:
        return type_name or ""
    when = time.strftime("%d %b %Y  %H:%M", time.localtime(info.st_mtime))
    if is_dir:
        return f"{type_name or 'Folder'}\n{when}"
    # Say so rather than showing an empty pane and leaving the viewer to
    # wonder why this one file previews as nothing.
    tail = "\nOnline only, not downloaded" if cloud_only else ""
    return (f"{type_name or 'File'}\n{human_size(info.st_size)}\n{when}"
            f"{tail}")


class Launcher:
    def __init__(self, controller, opacity=None, preview=True,
                 video_preview=True):
        enable_dpi_awareness()
        self.controller = controller
        self.preview_enabled = bool(preview)
        self._wheel_lines = wheel_lines()
        self.alpha = min(1.0, max(MIN_ALPHA, ALPHA if opacity is None else opacity))
        self.results = []
        # Where the viewport sits, and which result is selected. Both index
        # the results, not the widgets.
        self._top = 0
        self._cursor = -1
        self._column_px = 0
        self._after_id = None
        self._deepen_id = None
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
        self._video_pending = []
        self._video_runs = 0
        self._video_final = False
        self._video_index = 0
        self._video_ticks = 0
        self._video_id = None
        self._video_start = 0.0
        self._fine_timer = False
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
        self._char_px: dict[str, int] = {}
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
        self.tree.bind("<MouseWheel>", self._on_wheel)
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

        style.configure("QF.Vertical.TScrollbar", background=BORDER,
                        troughcolor=BG, bordercolor=BG, arrowcolor=BG,
                        darkcolor=BORDER, lightcolor=BORDER,
                        borderwidth=0, arrowsize=0, width=self.px(6))
        style.map("QF.Vertical.TScrollbar",
                  background=[("active", DIM), ("!active", BORDER)])
        style.layout("QF.Vertical.TScrollbar",
                     [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                         ("Vertical.Scrollbar.thumb",
                          {"expand": "1", "sticky": "nswe"})]})])

        self.tree = ttk.Treeview(self.body, style="QF.Treeview", show="tree",
                                 selectmode="browse", height=self.rows_visible)
        self.tree.column("#0", stretch=True, anchor="w")
        # The list is a viewport over the results, so the scrollbar is driven
        # from the result count rather than from the widget's own contents.
        self.scrollbar = ttk.Scrollbar(self.body, orient="vertical",
                                       style="QF.Vertical.TScrollbar",
                                       command=self._on_scrollbar)
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

    def _text_px(self, text: str) -> int:
        """Width of a string, from cached character widths.

        `font.measure` is a round trip into Tcl. Eliding forty rows with a
        binary search made about 560 of them and cost 78ms of every keystroke.
        Character advances are additive in these fonts, so summing cached
        widths gives pixel-identical answers 350 times faster.
        """
        widths = self._char_px
        total = 0
        for character in text:
            width = widths.get(character)
            if width is None:
                width = self.row_font.measure(character)
                widths[character] = width
            total += width
        return total

    def _elide(self, text: str, max_px: int, keep_tail: bool) -> str:
        """Trim text to fit, dropping characters from whichever end matters less."""
        if max_px <= 0:
            return ""
        if self._text_px(text) <= max_px:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            candidate = (ELLIPSIS + text[mid:]) if keep_tail else (text[:mid] + ELLIPSIS)
            fits = self._text_px(candidate) <= max_px
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

    def _column_for(self, results) -> int:
        """The shared folder column for a whole result set.

        Measured once per search over at most a few hundred rows. Recomputing
        it from whatever is in view would make the folders shuffle sideways
        every time the list scrolled.
        """
        available = self._row_budget()
        cap = int(available * 0.45)
        leads = [self._elide(r.name, cap - self.px(12), keep_tail=False)
                 for r in results[:COLUMN_SAMPLE_ROWS]]
        return min(cap, max((self._text_px(lead) for lead in leads), default=0)
                   + self._text_px("    "))

    def _format_rows(self, results, column=None) -> list[str]:
        """Name on the left, folders aligned to a shared column.

        A tree row is one string, so the gap is padded with spaces measured
        against the real font. The column is only as wide as the longest name
        actually present, capped at 45% -- a fixed column would strand short
        names beside a wall of space and leave paths nothing to elide into.
        """
        available = self._row_budget()
        cap = int(available * 0.45)
        measure = self._text_px

        leads = []
        for result in results:
            # No "[dir]" marker any more: the shell icon says what it is.
            leads.append(self._elide(result.name, cap - self.px(12),
                                     keep_tail=False))

        if column is None:
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
                    # Decoding a clip costs a couple of seconds. Sweeping down
                    # a list of videos would queue one per row, and the row you
                    # stopped on would wait behind all of them.
                    if token != self._preview_token:
                        continue
                    # Hand each run over as it lands: the preview starts
                    # playing after the first, about half a second in, and
                    # grows as the rest arrive. Returning False abandons the
                    # decode the moment the selection moves on.
                    def deliver(width, height, pngs, token=token):
                        if token != self._preview_token:
                            return False
                        self._done.put(("video", token, pngs))
                        return True

                    videopreview.segments(
                        path, size=box, count=VIDEO_SEGMENTS,
                        per_segment=VIDEO_PER_SEGMENT, fps=VIDEO_FPS,
                        on_segment=deliver)
                    # No more runs. A clip short enough to yield only one has
                    # to play rather than wait for a second that never comes.
                    self._done.put(("video", token, None))
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
        cloud_only = (not is_dir) and is_cloud_only(path)
        return {
            "path": path,
            "image": image,
            "is_thumbnail": is_thumbnail,
            "meta": describe(path, is_dir, type_name, cloud_only),
            "excerpt": "" if is_dir else read_excerpt(path),
            # Decoding a placeholder would pull the whole file down over the
            # network, so a cloud-only video keeps its still thumbnail.
            "is_video": (not is_dir) and videopreview.is_video(path)
                        and not cloud_only,
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
                    if token != self._preview_token:
                        continue
                    if pngs is None:
                        self._finish_video()
                    else:
                        self._extend_video(pngs)
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
        """Swap the still for a looping clip that is already complete."""
        self._stop_video()
        self._video_final = True
        self._extend_video(pngs)

    def _finish_video(self) -> None:
        """The decoder has no more runs; play whatever arrived."""
        self._video_final = True
        self._extend_video([])

    def _extend_video(self, pngs) -> None:
        """Add a decoded run, starting playback once there is enough to loop."""
        if pngs:
            self._video_pending.extend(pngs)
            self._video_runs += 1
        if self._video_frames:
            # Already looping. Turning a run into images costs about 10ms a
            # frame, which would stall the animation for a fifth of a second
            # every time one arrived, so they are converted one per tick
            # instead.
            return
        if self._video_runs < VIDEO_BUFFER_RUNS and not self._video_final:
            # Not enough to play without catching up with the decoder. The
            # still thumbnail stays up in the meantime.
            return
        if not self._convert_pending(VIDEO_OPENING_FRAMES):
            return
        self._video_index = 0
        self._video_ticks = 0
        self._video_start = time.monotonic()
        if not self._fine_timer:
            _fine_timer(True)
            self._fine_timer = True
        if not self.preview_image_label.winfo_ismapped():
            self.preview_image_label.pack(before=self.preview_name,
                                          pady=(self.px(14), self.px(8)))
        self._advance_video()

    def _convert_pending(self, limit) -> int:
        """Turn up to `limit` waiting frames into Tk images. Returns how many."""
        made = 0
        while self._video_pending and made < limit:
            data = self._video_pending.pop(0)
            try:
                self._video_frames.append(tk.PhotoImage(data=data))
            except tk.TclError:
                self._video_pending.clear()
                break
            made += 1
        return made

    def _advance_video(self) -> None:
        self._video_id = None
        if not self._video_frames or not self.alive():
            return
        if self._video_pending:
            self._convert_pending(VIDEO_CONVERT_PER_TICK)
        # A cursor, not a counter. Taking `counter % len` looked equivalent
        # until frames started arriving mid-playback: the counter and the
        # length then grew in step, the remainder stopped changing, and the
        # preview held one frame for as long as conversion lasted, nearly
        # three seconds on a clip measured here.
        if self._video_index >= len(self._video_frames):
            self._video_index = 0
        frame = self._video_frames[self._video_index]
        self._video_index = (self._video_index + 1) % len(self._video_frames)
        self._video_ticks += 1
        try:
            self.preview_image_label.configure(image=frame)
        except tk.TclError:
            return
        self._video_id = self.root.after(self._next_delay(), self._advance_video)

    def _next_delay(self) -> int:
        """Milliseconds until the next frame is due, against a fixed clock.

        Asking for a constant delay after each frame loses the time the frame
        itself took, and with Windows rounding every timer up to its 15.6ms
        tick, a nominal 12fps played at 10.7fps with visible jitter. Aiming at
        a fixed schedule instead holds the rate, and `_fine_timer` asks Windows
        for 1ms ticks so the schedule can be met.
        """
        due = self._video_start + self._video_ticks / VIDEO_FPS
        return max(1, int(round((due - time.monotonic()) * 1000)))

    def _stop_video(self) -> None:
        if self._video_id is not None:
            try:
                self.root.after_cancel(self._video_id)
            except Exception:
                pass
            self._video_id = None
        if self._fine_timer:
            # Every timeBeginPeriod needs its timeEndPeriod; a finer system
            # timer costs power, so it is held only while a clip is playing.
            _fine_timer(False)
            self._fine_timer = False
        # Dropping the PhotoImages matters: a full preview of a pane-width clip
        # is about a hundred frames and 50MB inside Tk.
        self._video_frames = []
        self._video_pending = []
        self._video_runs = 0
        self._video_final = False
        self._video_index = 0
        self._video_ticks = 0

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
        for attr in ("_after_id", "_preview_id", "_deepen_id"):
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
        if self._deepen_id is not None:
            # Still typing: the longer fetch would only be thrown away.
            try:
                self.root.after_cancel(self._deepen_id)
            except Exception:
                pass
            self._deepen_id = None
        self._after_id = self.root.after(DEBOUNCE_MS, self._run_search)

    def row_count(self) -> int:
        return len(self.results)

    def row_text(self, index: int) -> str:
        """The text of a row, whether or not it currently has a widget."""
        if 0 <= index < len(self.results):
            return self._format_rows([self.results[index]])[0]
        return ""

    def _run_search(self) -> None:
        self._after_id = None
        query = self.entry.get()
        self.results, note = self.controller.query(query)
        # One column width for the whole result set, measured once. Deriving
        # it from whatever happens to be on screen would make the folders
        # shuffle sideways as the list scrolled.
        self._column_px = self._column_for(self.results)
        # Back to the top. Leaving the view where the last search left it
        # showed an empty stretch of list and hid the match that had just been
        # selected.
        self._top = 0
        self._cursor = 0 if self.results else -1
        self._paint()
        if self.results:
            self._schedule_preview()
            self._schedule_deepen()
        else:
            self._clear_preview()
        self.status.configure(text=note)
        self._sync_panels()

    def _schedule_deepen(self) -> None:
        """Ask for the rest of the matches once typing has paused."""
        if not hasattr(self.controller, "more"):
            return
        if self._deepen_id is not None:
            try:
                self.root.after_cancel(self._deepen_id)
            except Exception:
                pass
        self._deepen_id = self.root.after(DEEPEN_MS, self._deepen)

    def _deepen(self) -> None:
        self._deepen_id = None
        query = self.entry.get()
        try:
            longer = self.controller.more(query)
        except Exception:
            return
        if not longer:
            return
        results, note = longer
        # Append what is new rather than swapping the list. A deeper search
        # interleaves repeated names over a larger pool, so its order differs
        # from the short one's; replacing outright would shuffle the rows
        # under the pointer. Keeping what is on screen and adding the rest
        # leaves the viewport, the selection and the ranking alone.
        seen = {r.path for r in self.results}
        extra = [r for r in results if r.path not in seen]
        if not extra:
            return
        self.results = self.results + extra
        self._paint()
        self.status.configure(text=note)

    # -- the viewport ------------------------------------------------------

    def _visible_rows(self) -> int:
        return max(1, min(self.rows_visible, len(self.results)))

    def _max_top(self) -> int:
        return max(0, len(self.results) - self._visible_rows())

    def _paint(self) -> None:
        """Write the rows in view into the handful of widgets that exist.

        The list is a viewport, not a copy: a query matching fifty thousand
        files still owns about a dozen tree items, and scrolling rewrites them
        rather than creating more. Painting a screenful costs about a
        millisecond, so it can happen on every wheel notch.
        """
        count = self._visible_rows() if self.results else 0
        items = self._ensure_items(count)
        rows = self.results[self._top:self._top + count]
        texts = self._format_rows(rows, column=self._column_px)
        self._row_keys = []
        for item, result, text in zip(items, rows, texts):
            key = self._icons.key_for(result.path, result.is_dir)
            self._row_keys.append(key)
            self.tree.item(item, text=text,
                           image=self._icon_images.get(key, self._blank))
            if key not in self._icon_images:
                # Only what is on screen is worth a shell lookup.
                self._request_icon(key, result.path, result.is_dir)
        self._paint_selection(items)
        self._sync_scrollbar()

    def _ensure_items(self, count: int):
        items = list(self.tree.get_children())
        while len(items) > count:
            self.tree.delete(items.pop())
        while len(items) < count:
            items.append(self.tree.insert("", "end", text="",
                                          image=self._blank))
        return items

    def _paint_selection(self, items) -> None:
        offset = self._cursor - self._top
        if 0 <= offset < len(items):
            self.tree.selection_set(items[offset])
            self.tree.focus(items[offset])
        elif items:
            self.tree.selection_remove(*items)

    def _sync_scrollbar(self) -> None:
        total = len(self.results)
        if total <= self._visible_rows():
            if self.scrollbar.winfo_ismapped():
                self.scrollbar.pack_forget()
            return
        if not self.scrollbar.winfo_ismapped():
            # Only shown when there is something to scroll: it doubles as the
            # signal that the list holds more than a screenful.
            self.scrollbar.pack(side="right", fill="y", before=self.tree)
        first = self._top / total
        self.scrollbar.set(first, (self._top + self._visible_rows()) / total)

    def _scroll_to(self, top: int) -> None:
        top = max(0, min(self._max_top(), int(top)))
        if top == self._top:
            return
        self._top = top
        self._paint()

    def _on_wheel(self, event) -> str:
        """Scroll by however many rows Windows is configured for."""
        notches = -event.delta / 120.0
        self._scroll_to(self._top + notches * self._wheel_lines)
        return "break"

    def _on_scrollbar(self, action, value, unit=None) -> None:
        """Drive the viewport from the scrollbar."""
        if action == "moveto":
            self._scroll_to(round(float(value) * len(self.results)))
        elif action == "scroll":
            step = self._visible_rows() if unit == "pages" else 1
            self._scroll_to(self._top + int(value) * step)

    def set_status(self, text: str) -> None:
        if not self.alive():
            return
        self.status.configure(text=text)
        if self._visible:
            self._sync_panels()

    # -- interaction -------------------------------------------------------

    def _selected(self) -> int:
        """Index into the results, not into the widgets on screen."""
        if 0 <= self._cursor < len(self.results):
            return self._cursor
        return -1

    def _select(self, target: int) -> None:
        if not self.results:
            return
        target = max(0, min(len(self.results) - 1, int(target)))
        if target == self._cursor:
            return
        self._cursor = target
        self._reveal_cursor()
        self._paint()
        self._schedule_preview()

    def _reveal_cursor(self) -> None:
        """Move the viewport the least it can to put the cursor on screen."""
        visible = self._visible_rows()
        if self._cursor < self._top:
            self._top = self._cursor
        elif self._cursor >= self._top + visible:
            self._top = self._cursor - visible + 1
        self._top = max(0, min(self._max_top(), self._top))

    def _move(self, delta: int) -> str:
        if not self.results:
            return "break"
        current = self._selected()
        self._select((current if current >= 0 else 0) + delta)
        return "break"

    def _row_at(self, y: int) -> int:
        """Which result is under the pointer, counting from the viewport."""
        item = self.tree.identify_row(y)
        if not item:
            return -1
        try:
            return self._top + self.tree.index(item)
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
