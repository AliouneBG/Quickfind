"""Windows shell icons and thumbnails, converted to PNG for Tk.

Tk cannot take an HICON, and Tk 8.6 reads PNG but not JPEG, so everything the
shell hands back is turned into PNG bytes here with zlib. That keeps the whole
project on the standard library.

Two sources, deliberately different:

* ``icon_png`` uses SHGetFileInfo with SHGFI_USEFILEATTRIBUTES, which resolves
  an icon from an extension without touching the disk. Rows are drawn on every
  keystroke, so this has to be cheap and cacheable.
* ``thumbnail_png`` uses IShellItemImageFactory, which reads the real file and
  returns an actual thumbnail -- the photo, the video frame, the PDF page.
  That costs I/O, so it is only ever called for a single hovered row.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import zlib
from ctypes import wintypes

SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
SHGFI_USEFILEATTRIBUTES = 0x000000010
SHGFI_TYPENAME = 0x000000400

FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_DIRECTORY = 0x10

DIB_RGB_COLORS = 0
BI_RGB = 0

SIIGBF_RESIZETOFIT = 0x00000000
SIIGBF_THUMBNAILONLY = 0x00000008

COINIT_APARTMENTTHREADED = 0x2


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wintypes.HICON), ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]

    def __init__(self, text):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, ctypes.byref(self))


IID_IShellItemImageFactory = "{BCC18B79-BA16-442F-80C4-8A59C30C463B}"

# Handles are pointer-sized. Without explicit prototypes ctypes assumes a
# 32-bit int and every handle above 2GB raises OverflowError.
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetObjectW.restype = ctypes.c_int
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                             wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                             wintypes.UINT]
_gdi32.GetDIBits.restype = ctypes.c_int

_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.c_void_p]
_user32.GetIconInfo.restype = wintypes.BOOL
_user32.DestroyIcon.argtypes = [wintypes.HICON]

_shell32.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.UINT,
                                    wintypes.UINT]
_shell32.SHGetFileInfoW.restype = ctypes.c_void_p
_shell32.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_shell32.SHCreateItemFromParsingName.restype = ctypes.c_long


# ---------------------------------------------------------------------------
# PNG encoding
# ---------------------------------------------------------------------------

def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Minimal RGBA PNG encoder; Tk 8.6 reads PNG natively."""
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)  # filter type 0
        rows += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (struct.pack(">I", len(payload)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))


def _bitmap_to_rgba(hbitmap):
    """Read a 32-bit top-down copy of an HBITMAP and swap BGRA to RGBA."""
    gdi32, user32 = _gdi32, _user32

    bitmap = BITMAP()
    if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(BITMAP), ctypes.byref(bitmap)):
        return None, 0, 0
    width, height = bitmap.bmWidth, bitmap.bmHeight
    if width <= 0 or height <= 0:
        return None, 0, 0

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # negative: top-down rows
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    buffer = ctypes.create_string_buffer(width * height * 4)
    dc = user32.GetDC(None)
    try:
        copied = gdi32.GetDIBits(dc, hbitmap, 0, height, buffer,
                                 ctypes.byref(info), DIB_RGB_COLORS)
    finally:
        user32.ReleaseDC(None, dc)
    if not copied:
        return None, 0, 0

    pixels = bytearray(buffer.raw)
    pixels[0::4], pixels[2::4] = pixels[2::4], pixels[0::4]  # BGRA -> RGBA
    return pixels, width, height


def _apply_mask(pixels, hbmMask, width, height) -> None:
    """Some icons carry no alpha; derive it from the AND mask instead."""
    mask, mask_w, mask_h = _bitmap_to_rgba(hbmMask)
    if mask is None or mask_w != width:
        pixels[3::4] = b"\xff" * (len(pixels) // 4)
        return
    for i in range(width * min(height, mask_h)):
        # In the mask, white means transparent.
        pixels[i * 4 + 3] = 0 if mask[i * 4] else 255


def _icon_to_png(hicon) -> bytes | None:
    gdi32, user32 = _gdi32, _user32

    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    try:
        pixels, width, height = _bitmap_to_rgba(info.hbmColor)
        if pixels is None:
            return None
        if not any(pixels[3::4]):
            _apply_mask(pixels, info.hbmMask, width, height)
        return encode_png(width, height, bytes(pixels))
    finally:
        for handle in (info.hbmColor, info.hbmMask):
            if handle:
                gdi32.DeleteObject(handle)


# ---------------------------------------------------------------------------
# Icons (cheap, extension-based)
# ---------------------------------------------------------------------------

def icon_png(path: str, is_dir: bool = False, large: bool = False,
             use_attributes: bool = True):
    """PNG bytes and the shell's type name for a path.

    With ``use_attributes`` the shell answers from the extension alone and never
    touches the disk, which is what makes this safe to call while rendering.
    """
    info = SHFILEINFOW()
    flags = SHGFI_ICON | SHGFI_TYPENAME
    flags |= SHGFI_LARGEICON if large else SHGFI_SMALLICON
    attributes = 0
    if use_attributes:
        flags |= SHGFI_USEFILEATTRIBUTES
        attributes = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL

    result = _shell32.SHGetFileInfoW(
        path, attributes, ctypes.byref(info), ctypes.sizeof(info), flags)
    if not result or not info.hIcon:
        return None, ""
    try:
        return _icon_to_png(info.hIcon), info.szTypeName
    finally:
        _user32.DestroyIcon(info.hIcon)


# Icons live in the extension, except where the file carries its own.
PER_FILE_ICONS = frozenset({".exe", ".lnk", ".ico", ".cpl", ".msc", ".scr"})


class IconCache:
    """Extension-keyed icon cache; the PhotoImage is built by the UI thread."""

    def __init__(self):
        self._png = {}
        self._types = {}
        self._lock = threading.Lock()

    def key_for(self, path: str, is_dir: bool) -> str:
        if is_dir:
            return "<dir>"
        extension = os.path.splitext(path)[1].lower()
        if extension in PER_FILE_ICONS:
            return path.lower()
        return extension or "<none>"

    def png_for(self, path: str, is_dir: bool, large: bool = False):
        key = ("L" if large else "S") + self.key_for(path, is_dir)
        with self._lock:
            if key in self._png:
                return self._png[key], self._types.get(key, "")

        extension = os.path.splitext(path)[1].lower()
        per_file = extension in PER_FILE_ICONS and os.path.exists(path)
        probe = path if (per_file or is_dir) else ("x" + extension)
        data, type_name = icon_png(probe, is_dir, large,
                                   use_attributes=not per_file)

        with self._lock:
            self._png[key] = data
            self._types[key] = type_name
        return data, type_name


# ---------------------------------------------------------------------------
# Thumbnails (real file contents, for the preview pane)
# ---------------------------------------------------------------------------

def thumbnail_png(path: str, size: int = 256,
                  thumbnail_only: bool = False) -> bytes | None:
    """A real thumbnail via IShellItemImageFactory, or None.

    With ``thumbnail_only`` the shell refuses to substitute a generic file-type
    icon, so the caller can tell "here is the actual picture" apart from "here
    is a blank page glyph" and spend the space accordingly.

    Call this off the UI thread: it reads the file. COM is initialised per
    call because the caller is a short-lived worker thread.
    """
    ole32 = ctypes.windll.ole32
    initialised = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    try:
        factory = ctypes.c_void_p()
        iid = GUID(IID_IShellItemImageFactory)
        hresult = _shell32.SHCreateItemFromParsingName(
            ctypes.c_wchar_p(os.path.abspath(path)), None,
            ctypes.byref(iid), ctypes.byref(factory))
        if hresult != 0 or not factory:
            return None
        try:
            vtable = ctypes.cast(
                ctypes.cast(factory, ctypes.POINTER(ctypes.c_void_p))[0],
                ctypes.POINTER(ctypes.c_void_p))
            get_image = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                ctypes.POINTER(wintypes.HBITMAP))(vtable[3])
            release = ctypes.WINFUNCTYPE(
                ctypes.c_ulong, ctypes.c_void_p)(vtable[2])

            flags = SIIGBF_RESIZETOFIT
            if thumbnail_only:
                flags |= SIIGBF_THUMBNAILONLY
            hbitmap = wintypes.HBITMAP()
            hresult = get_image(factory, SIZE(size, size), flags,
                                ctypes.byref(hbitmap))
            release(factory)
            if hresult != 0 or not hbitmap:
                return None
            try:
                pixels, width, height = _bitmap_to_rgba(hbitmap)
                if pixels is None:
                    return None
                if not any(pixels[3::4]):
                    pixels[3::4] = b"\xff" * (len(pixels) // 4)
                return encode_png(width, height, bytes(pixels))
            finally:
                _gdi32.DeleteObject(hbitmap)
        except Exception:
            return None
    except Exception:
        return None
    finally:
        if initialised in (0, 1):
            ole32.CoUninitialize()
