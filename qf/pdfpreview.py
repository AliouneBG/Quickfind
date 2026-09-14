"""The first page of a PDF, rendered by the renderer Windows already ships.

Explorer shows a page thumbnail for PDFs only when something has registered a
thumbnail handler, which on a machine without Acrobat is nothing at all: asking
the shell for one returns empty and the pane falls back to a generic icon. So
the page is rendered here instead.

`Windows.Data.Pdf` is the renderer Edge uses, present on every Windows 10 and
11. It is WinRT rather than COM, which means activation factories, methods
found by vtable slot, and asynchronous calls that have to be waited on. All of
that is done here through ctypes, so the project still needs nothing installed.
"""
from __future__ import annotations

import ctypes
import os
import struct
import tempfile
import time
from ctypes import wintypes

combase = ctypes.WinDLL("combase")
ole32 = ctypes.WinDLL("ole32")

RO_INIT_MULTITHREADED = 1
S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = -2147417850          # 0x80010106
ASYNC_STARTED = 0
ASYNC_COMPLETED = 1
FILE_ACCESS_READ_WRITE = 1

# Waiting is a poll rather than a completion handler: a handler means building
# a COM object with a vtable in ctypes, and a render takes a tenth of a second.
POLL_SECONDS = 0.002
DEFAULT_TIMEOUT = 8.0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]

    def __init__(self, text=None):
        super().__init__()
        if text:
            ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))


IID_STORAGE_FILE_STATICS = GUID("{5984C710-DAF2-43C8-8BB4-A4D3EACFD03F}")
IID_PDF_DOCUMENT_STATICS = GUID("{433A0B5F-C007-4788-90F2-08143D922599}")
IID_ASYNC_INFO = GUID("{00000036-0000-0000-C000-000000000046}")
IID_CLOSABLE = GUID("{30D5A829-7FA4-4026-83BB-D75BAE4EA99E}")

CLASS_STORAGE_FILE = "Windows.Storage.StorageFile"
CLASS_PDF_DOCUMENT = "Windows.Data.Pdf.PdfDocument"
CLASS_RENDER_OPTIONS = "Windows.Data.Pdf.PdfPageRenderOptions"

# Every WinRT interface inherits IInspectable, which occupies slots 0 to 5, so
# an interface's own methods start at 6 in declaration order.
QUERY_INTERFACE = 0
RELEASE = 2
STORAGE_GET_FILE_FROM_PATH = 6
STORAGE_FILE_OPEN = 8
PDF_LOAD_FROM_FILE = 6
PDF_GET_PAGE = 6
PDF_GET_PAGE_COUNT = 7
PAGE_RENDER_WITH_OPTIONS = 7
PAGE_GET_SIZE = 10
OPTIONS_PUT_DESTINATION_WIDTH = 9
ASYNC_GET_RESULTS = 8
ASYNC_INFO_GET_STATUS = 7
CLOSABLE_CLOSE = 6

combase.RoInitialize.argtypes = [ctypes.c_int]
combase.RoInitialize.restype = ctypes.c_long
combase.RoUninitialize.argtypes = []
combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint,
                                        ctypes.POINTER(ctypes.c_void_p)]
combase.WindowsCreateString.restype = ctypes.c_long
combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
combase.RoGetActivationFactory.restype = ctypes.c_long
combase.RoActivateInstance.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
combase.RoActivateInstance.restype = ctypes.c_long


class Size(ctypes.Structure):
    """Windows.Foundation.Size: a page's dimensions in points."""
    _fields_ = [("width", ctypes.c_float), ("height", ctypes.c_float)]


# What the renderer gives back per unit of DestinationWidth. It came back at
# exactly twice the requested width on the machine this was written on, and
# assuming that everywhere would crop the pane on a machine where it is 1. So
# it is measured from the first page rendered and remembered.
_scale = None


def is_pdf(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".pdf"


def _method(pointer, index, restype, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0]
    slot = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(slot)


def _release(pointer):
    if pointer:
        try:
            _method(pointer, RELEASE, ctypes.c_ulong)(pointer)
        except Exception:
            pass


class _HString:
    """A WinRT string, which has to be created and deleted rather than passed."""

    def __init__(self, text):
        self.handle = ctypes.c_void_p()
        if combase.WindowsCreateString(text, len(text),
                                       ctypes.byref(self.handle)) != 0:
            raise OSError("WindowsCreateString failed")

    def __enter__(self):
        return self.handle

    def __exit__(self, *exc):
        combase.WindowsDeleteString(self.handle)


def _factory(class_name, iid):
    with _HString(class_name) as name:
        out = ctypes.c_void_p()
        if combase.RoGetActivationFactory(name, ctypes.byref(iid),
                                          ctypes.byref(out)) != 0:
            raise OSError("no activation factory for " + class_name)
        return out


def _wait(operation, timeout=DEFAULT_TIMEOUT):
    """Block until an asynchronous call finishes, or give up."""
    info = ctypes.c_void_p()
    if _method(operation, QUERY_INTERFACE, ctypes.c_long,
               ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            operation, ctypes.byref(IID_ASYNC_INFO), ctypes.byref(info)) != 0:
        raise OSError("operation has no IAsyncInfo")
    try:
        status = ctypes.c_int(ASYNC_STARTED)
        get_status = _method(info, ASYNC_INFO_GET_STATUS, ctypes.c_long,
                             ctypes.POINTER(ctypes.c_int))
        deadline = time.monotonic() + timeout
        while True:
            if get_status(info, ctypes.byref(status)) != 0:
                raise OSError("could not read the operation's status")
            if status.value != ASYNC_STARTED:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("the operation never finished")
            time.sleep(POLL_SECONDS)
        if status.value != ASYNC_COMPLETED:
            raise OSError("the operation failed")
    finally:
        _release(info)


def _result(operation):
    """Wait for an operation and hand back what it produced."""
    _wait(operation)
    out = ctypes.c_void_p()
    if _method(operation, ASYNC_GET_RESULTS, ctypes.c_long,
               ctypes.POINTER(ctypes.c_void_p))(
            operation, ctypes.byref(out)) != 0:
        raise OSError("could not read the operation's result")
    return out


def _finish(action):
    """Wait for an operation that produces nothing."""
    _wait(action)
    _method(action, ASYNC_GET_RESULTS, ctypes.c_long)(action)


def _storage_file(statics, path):
    with _HString(os.path.abspath(path)) as name:
        operation = ctypes.c_void_p()
        # A relative path is rejected outright, which is worth knowing: the
        # failure arrives as a failed async rather than a bad argument.
        if _method(statics, STORAGE_GET_FILE_FROM_PATH, ctypes.c_long,
                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                statics, name, ctypes.byref(operation)) != 0:
            raise OSError("could not ask for " + path)
        try:
            return _result(operation)
        finally:
            _release(operation)


def _close(stream):
    closer = ctypes.c_void_p()
    if _method(stream, QUERY_INTERFACE, ctypes.c_long, ctypes.POINTER(GUID),
               ctypes.POINTER(ctypes.c_void_p))(
            stream, ctypes.byref(IID_CLOSABLE), ctypes.byref(closer)) == 0:
        # Without this the rendered bytes can still be sitting in the stream
        # rather than in the file we are about to read.
        _method(closer, CLOSABLE_CLOSE, ctypes.c_long)(closer)
        _release(closer)


def _fit(page: Size, box) -> int:
    """The width that makes the page fit the box, keeping its proportions."""
    width, height = box
    if page.width <= 0 or page.height <= 0:
        return int(width)
    scale = min(width / page.width, height / page.height)
    return max(8, int(page.width * scale))


def _render(storage, page, handles, wanted):
    """Draw the page into a temporary file and hand back the PNG bytes.

    The renderer writes into a stream, and a stream over a real file is the
    one kind that can be obtained without also building buffers and readers
    out of ctypes. The file is deleted before returning.
    """
    global _scale
    handle, output = tempfile.mkstemp(prefix="quickfind-pdf-", suffix=".png")
    os.close(handle)
    try:
        target = _storage_file(storage, output)
        handles.append(target)
        operation = ctypes.c_void_p()
        if _method(target, STORAGE_FILE_OPEN, ctypes.c_long, ctypes.c_int,
                   ctypes.POINTER(ctypes.c_void_p))(
                target, FILE_ACCESS_READ_WRITE, ctypes.byref(operation)) != 0:
            return None
        handles.append(operation)
        stream = _result(operation)
        handles.append(stream)

        options = ctypes.c_void_p()
        with _HString(CLASS_RENDER_OPTIONS) as name:
            if combase.RoActivateInstance(name, ctypes.byref(options)) != 0:
                return None
        handles.append(options)
        asked = max(8, int(wanted / (_scale or 1.0)))
        _method(options, OPTIONS_PUT_DESTINATION_WIDTH, ctypes.c_long,
                ctypes.c_uint32)(options, asked)

        action = ctypes.c_void_p()
        if _method(page, PAGE_RENDER_WITH_OPTIONS, ctypes.c_long,
                   ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p))(
                page, stream, options, ctypes.byref(action)) != 0:
            return None
        handles.append(action)
        _finish(action)
        _close(stream)

        with open(output, "rb") as fh:
            png = fh.read()
        if len(png) < 24 or not png.startswith(b"\x89PNG"):
            return None
        drawn = struct.unpack(">I", png[16:20])[0]
        if drawn and asked:
            _scale = drawn / asked
        return png
    finally:
        try:
            os.remove(output)
        except OSError:
            pass


def first_page(path: str, box=(440, 340), timeout=DEFAULT_TIMEOUT):
    """Render page one. See `render_page`."""
    return render_page(path, 0, box=box, timeout=timeout)


def render_page(path: str, index: int = 0, box=(440, 340),
                timeout=DEFAULT_TIMEOUT):
    """Render one page as PNG bytes. Returns ``(png, page_count)``.

    ``(None, 0)`` when the file cannot be rendered, which covers a missing
    file, something that is not really a PDF, and one that is password
    protected; ``(None, count)`` when the file is fine but has no such page,
    so a caller paging past the end still learns where the end is. Safe to
    call only off the UI thread: it reads and renders.

    Rendering large costs no more than rendering small -- measured across a
    dozen real documents, 86ms at 900px against 75ms at 234px -- because
    nearly all of it is opening and parsing the file rather than rasterising.
    """
    global _scale
    started = combase.RoInitialize(RO_INIT_MULTITHREADED)
    # Already initialised, in either mode, is fine: the apartment exists, which
    # is all this needs. Only undo what this call actually did.
    if started not in (S_OK, S_FALSE, RPC_E_CHANGED_MODE):
        return None, 0

    handles = []
    try:
        storage = _factory(CLASS_STORAGE_FILE, IID_STORAGE_FILE_STATICS)
        handles.append(storage)
        pdf = _factory(CLASS_PDF_DOCUMENT, IID_PDF_DOCUMENT_STATICS)
        handles.append(pdf)

        source = _storage_file(storage, path)
        handles.append(source)
        operation = ctypes.c_void_p()
        if _method(pdf, PDF_LOAD_FROM_FILE, ctypes.c_long, ctypes.c_void_p,
                   ctypes.POINTER(ctypes.c_void_p))(
                pdf, source, ctypes.byref(operation)) != 0:
            return None, 0
        handles.append(operation)
        document = _result(operation)
        handles.append(document)

        pages = ctypes.c_uint32(0)
        _method(document, PDF_GET_PAGE_COUNT, ctypes.c_long,
                ctypes.POINTER(ctypes.c_uint32))(document, ctypes.byref(pages))
        if not pages.value:
            return None, 0

        if not 0 <= index < pages.value:
            return None, pages.value

        page = ctypes.c_void_p()
        if _method(document, PDF_GET_PAGE, ctypes.c_long, ctypes.c_uint32,
                   ctypes.POINTER(ctypes.c_void_p))(
                document, index, ctypes.byref(page)) != 0:
            return None, pages.value
        handles.append(page)

        size = Size()
        _method(page, PAGE_GET_SIZE, ctypes.c_long, ctypes.POINTER(Size))(
            page, ctypes.byref(size))
        wanted = _fit(size, box)

        png = _render(storage, page, handles, wanted)
        if png is None:
            return None, pages.value
        drawn = struct.unpack(">I", png[16:20])[0]
        if drawn > wanted * 1.05 and _scale:
            # The first page of a session is rendered before the scale is
            # known, so it can come back too big for the pane. Now that it is
            # known, draw it once more at the size actually wanted.
            # The same target: `_render` applies the scale it has just learned.
            again = _render(storage, page, handles, wanted)
            if again is not None:
                png = again
        return png, pages.value
    except Exception:
        return None, 0
    finally:
        for handle in reversed(handles):
            _release(handle)
        if started in (S_OK, S_FALSE):
            combase.RoUninitialize()
