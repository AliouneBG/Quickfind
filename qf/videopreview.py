"""Short, silent video previews, decoded with Media Foundation via ctypes.

Tk has no video widget, so a "preview" here is a handful of real frames decoded
out of the file and cycled as images. Only the video stream is ever read, so
there is no audio to mute.

Two things make this fast enough to run on a hover:

* The reader is asked for a small RGB32 output rather than the native size.
  Letting Media Foundation scale during conversion took a 1920x1080 decode from
  1819ms to 114ms.
* Frames are read consecutively after a single seek. Seeking to each frame in
  turn costs up to 60 decodes per seek, because the reader has to run forward
  from the preceding keyframe.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

from .shellicon import encode_png

mfplat = ctypes.WinDLL("mfplat")
mfreadwrite = ctypes.WinDLL("mfreadwrite")
ole32 = ctypes.WinDLL("ole32")

MF_VERSION = 0x00020070
MFSTARTUP_NOSOCKET = 1
COINIT_APARTMENTTHREADED = 0x2

FIRST_VIDEO_STREAM = 0xFFFFFFFC
MEDIASOURCE = 0xFFFFFFFF
VT_I8 = 20
ENDOFSTREAM = 0x00000002

VIDEO_EXTENSIONS = frozenset(
    ".mp4 .m4v .mkv .mov .avi .wmv .webm .mpg .mpeg .ts .m2ts .flv .3gp".split())

# A preview is several short runs from across the clip rather than one long
# one: one continuous second tells you about one moment, six spread through
# the film tell you what it is. Defaults are 6 runs of 9 frames at 12fps,
# about 4.5 seconds of playback.
DEFAULT_SEGMENTS = 6
DEFAULT_PER_SEGMENT = 12
DEFAULT_FPS = 12.0
# A clip barely longer than the preview is better shown straight through than
# chopped into runs with gaps between them.
SHORT_CLIP_FACTOR = 1.5
DEFAULT_FRAMES = 24        # when a caller wants one run instead
# Where to sample from. Trimming both ends drops title cards and credits, and
# the bias bunches samples toward the middle.
FIRST_FRACTION = 0.12
LAST_FRACTION = 0.90
CENTRE_BIAS = 1.2
MIN_SEEKABLE_SECONDS = 1.0
START_FRACTION = 0.15      # skip title cards and black leader
MAX_READS_PER_FRAME = 40
# How many decoded frames may be thrown away while running from the keyframe
# forward to the requested start time, and how many opening frames may be
# skipped for being a flat colour.
MAX_CATCH_UP_READS = 60
MAX_BLANK_SKIPS = 8
BLANK_RANGE = 10           # a frame this flat carries no information
# A 4K clip decodes far slower than a 720p one. Rather than stall the
# preview worker, stop early and animate however many frames arrived. Runs are
# handed over as they finish, so a preview is already playing well before this.
DEFAULT_BUDGET_SECONDS = 6.0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]

    def __init__(self, text=None):
        super().__init__()
        if text:
            ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(self))


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_longlong), ("pad", ctypes.c_longlong)]


MF_MT_MAJOR_TYPE = GUID("{48eba18e-f8c9-4687-bf11-0a74c9f96a8f}")
MF_MT_SUBTYPE = GUID("{f7e34c9a-42e8-4714-b74b-cb29d72c35e5}")
MF_MT_FRAME_SIZE = GUID("{1652c33d-d6b2-4012-b834-72030849a37d}")
MFMediaType_Video = GUID("{73646976-0000-0010-8000-00AA00389B71}")
MFVideoFormat_RGB32 = GUID("{00000016-0000-0010-8000-00AA00389B71}")
MF_PD_DURATION = GUID("{6c990d33-bb8e-477a-8598-0d5d96fcd88a}")
# Row order is not fixed: a negative stride means the buffer starts at the
# bottom row. A webm decoded upright while an mkv came out upside down, so this
# has to be read per file rather than assumed.
MF_MT_DEFAULT_STRIDE = GUID("{644b4e48-1e02-4516-b0eb-c01ca9d49ac6}")
# Without this the reader only offers the codec's native format (usually NV12)
# and any request for RGB32 comes back as MF_E_INVALIDMEDIATYPE.
MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING = GUID(
    "{0f81da2c-b537-4672-a8b2-a681b17307a3}")
GUID_NULL = GUID()

# COM vtable slots.
IUNKNOWN_RELEASE = 2
AT_GET_UINT32 = 7
AT_GET_UINT64 = 8
AT_SET_UINT32 = 21
AT_SET_UINT64 = 22
AT_SET_GUID = 24
SR_GET_NATIVE_MEDIA_TYPE = 5
SR_GET_CURRENT_MEDIA_TYPE = 6
SR_SET_CURRENT_MEDIA_TYPE = 7
SR_SET_CURRENT_POSITION = 8
SR_READ_SAMPLE = 9
SR_GET_PRESENTATION_ATTRIBUTE = 12
SAMPLE_CONVERT_CONTIGUOUS = 41
BUF_LOCK = 3
BUF_UNLOCK = 4


def _method(pointer, index, restype, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0]
    slot = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(slot)


def _release(pointer):
    if pointer:
        try:
            _method(pointer, IUNKNOWN_RELEASE, ctypes.c_ulong)(pointer)
        except Exception:
            pass


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS


def _is_blank(raw: bytes, step: int = 4 * 97) -> bool:
    """Is this frame a flat colour? Sampled, so it costs almost nothing.

    The step is a multiple of four so that every sample is the same channel:
    the fourth byte of RGB32 is padding and is always zero, and mixing it in
    would make a white frame look like a high-contrast one.
    """
    sample = raw[::step]
    if not sample:
        return True
    return max(sample) - min(sample) <= BLANK_RANGE


def _bgra_to_png(raw: bytes, width: int, height: int, flip: bool) -> bytes:
    """Media Foundation hands back BGRA; PNG wants RGBA."""
    stride = width * 4
    pixels = bytearray(raw[:stride * height])
    pixels[0::4], pixels[2::4] = pixels[2::4], pixels[0::4]
    # The fourth channel of RGB32 is padding, not alpha, and arrives as zeroes.
    pixels[3::4] = b"\xff" * (len(pixels) // 4)
    if flip:
        pixels = b"".join(bytes(pixels[y * stride:(y + 1) * stride])
                          for y in range(height - 1, -1, -1))
    return encode_png(width, height, bytes(pixels))


def positions(duration, count, first=FIRST_FRACTION, last=LAST_FRACTION,
              bias=CENTRE_BIAS):
    """Where in the clip to sample, in seconds, weighted toward the middle.

    Title cards, logos and credits are the least informative part of a video,
    so the range is trimmed at both ends. The power curve then bunches the
    samples toward the middle, where whatever the video is actually about
    tends to be.
    """
    if count < 1:
        return []
    if not duration or duration <= 0:
        return [0.0] * count
    if count == 1:
        return [duration * first]
    out = []
    for i in range(count):
        x = i / (count - 1)
        sign = 1.0 if x >= 0.5 else -1.0
        curved = 0.5 + 0.5 * sign * abs(2.0 * x - 1.0) ** bias
        out.append(duration * (first + curved * (last - first)))
    return out


def frames(path, size=(240, 135), count=DEFAULT_FRAMES, fps=DEFAULT_FPS,
           start_fraction=START_FRACTION, flip=True,
           budget_seconds=DEFAULT_BUDGET_SECONDS):
    """One run of consecutive frames from a single point in the clip."""
    width, height, runs = segments(
        path, size=size, count=1, per_segment=count, fps=fps,
        first=start_fraction, budget_seconds=budget_seconds)
    return width, height, (runs[0] if runs else [])


def segments(path, size=(240, 135), count=DEFAULT_SEGMENTS,
             per_segment=DEFAULT_PER_SEGMENT, fps=DEFAULT_FPS,
             first=FIRST_FRACTION, last=LAST_FRACTION,
             budget_seconds=DEFAULT_BUDGET_SECONDS, on_segment=None):
    """Decode short runs from several points in the clip.

    Returns ``(width, height, [[png, ...], ...])``, one list per point
    sampled, and empty when the file cannot be decoded. ``on_segment`` is
    called with ``(width, height, pngs)`` as each run finishes, so a caller
    can start showing motion before the rest arrives; returning False from it
    stops the decode. Safe to call only off the UI thread: it reads and
    decodes.
    """
    initialised = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if mfplat.MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET) != 0:
        if initialised in (0, 1):
            ole32.CoUninitialize()
        return 0, 0, []

    reader = ctypes.c_void_p()
    runs = []
    flip = True
    width = height = 0
    try:
        attributes = ctypes.c_void_p()
        mfplat.MFCreateAttributes.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint]
        if mfplat.MFCreateAttributes(ctypes.byref(attributes), 1) != 0:
            return 0, 0, []
        _method(attributes, AT_SET_UINT32, ctypes.c_long,
                ctypes.POINTER(GUID), ctypes.c_uint)(
            attributes,
            ctypes.byref(MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING), 1)

        mfreadwrite.MFCreateSourceReaderFromURL.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        hr = mfreadwrite.MFCreateSourceReaderFromURL(
            os.path.abspath(path), attributes, ctypes.byref(reader))
        _release(attributes)
        if hr != 0 or not reader:
            return 0, 0, []

        media = ctypes.c_void_p()
        mfplat.MFCreateMediaType.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        if mfplat.MFCreateMediaType(ctypes.byref(media)) != 0:
            return 0, 0, []
        try:
            set_guid = _method(media, AT_SET_GUID, ctypes.c_long,
                               ctypes.POINTER(GUID), ctypes.POINTER(GUID))
            set_guid(media, ctypes.byref(MF_MT_MAJOR_TYPE),
                     ctypes.byref(MFMediaType_Video))
            set_guid(media, ctypes.byref(MF_MT_SUBTYPE),
                     ctypes.byref(MFVideoFormat_RGB32))
            if size:
                # Fit inside the box rather than filling it: a fixed output
                # size makes the reader stretch, so a 16:9 clip would come out
                # distorted in a 4:3 pane.
                target = _fit(_native_size(reader), size)
                _method(media, AT_SET_UINT64, ctypes.c_long,
                        ctypes.POINTER(GUID), ctypes.c_ulonglong)(
                    media, ctypes.byref(MF_MT_FRAME_SIZE),
                    (target[0] << 32) | target[1])
                # Demand top-down rows rather than detecting afterwards. The
                # output type publishes no stride to read (it answers
                # MF_E_ATTRIBUTENOTFOUND), and the default differed between a
                # webm and an mkv, so one of them always came out inverted.
                _method(media, AT_SET_UINT32, ctypes.c_long,
                        ctypes.POINTER(GUID), ctypes.c_uint)(
                    media, ctypes.byref(MF_MT_DEFAULT_STRIDE), target[0] * 4)
            hr = _method(reader, SR_SET_CURRENT_MEDIA_TYPE, ctypes.c_long,
                         ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(
                reader, FIRST_VIDEO_STREAM, None, media)
            if hr != 0:
                return 0, 0, []
        finally:
            _release(media)

        current = ctypes.c_void_p()
        if _method(reader, SR_GET_CURRENT_MEDIA_TYPE, ctypes.c_long,
                   ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
                reader, FIRST_VIDEO_STREAM, ctypes.byref(current)) != 0:
            return 0, 0, []
        try:
            packed = ctypes.c_ulonglong()
            _method(current, AT_GET_UINT64, ctypes.c_long,
                    ctypes.POINTER(GUID),
                    ctypes.POINTER(ctypes.c_ulonglong))(
                current, ctypes.byref(MF_MT_FRAME_SIZE), ctypes.byref(packed))
            width = int(packed.value >> 32)
            height = int(packed.value & 0xFFFFFFFF)
            stride = _default_stride(current)
            if stride is not None:
                flip = stride < 0
            else:
                # Nothing published; we asked for top-down above.
                flip = False
        finally:
            _release(current)
        if width <= 0 or height <= 0:
            return 0, 0, []

        duration = _duration(reader)
        read = _method(reader, SR_READ_SAMPLE, ctypes.c_long,
                       ctypes.c_ulong, ctypes.c_ulong,
                       ctypes.POINTER(ctypes.c_ulong),
                       ctypes.POINTER(ctypes.c_ulong),
                       ctypes.POINTER(ctypes.c_longlong),
                       ctypes.POINTER(ctypes.c_void_p))

        # Without a duration there is nowhere to seek to, so take one run from
        # wherever the file starts.
        if not duration or duration <= MIN_SEEKABLE_SECONDS:
            starts = [None]
        elif duration < (count * per_segment / fps) * SHORT_CLIP_FACTOR:
            # Barely longer than the preview itself: play it through instead of
            # cutting six one second holes in it.
            starts = [duration * first]
            per_segment *= count
        else:
            starts = positions(duration, count, first, last)
        deadline = time.monotonic() + budget_seconds
        for start in starts:
            pngs = _run(reader, read, start, per_segment, fps,
                        width, height, flip, deadline)
            if not pngs:
                break
            runs.append(pngs)
            if on_segment is not None and on_segment(width, height, pngs) is False:
                break
            if time.monotonic() > deadline:
                break
    except Exception:
        return width, height, runs
    finally:
        _release(reader)
        mfplat.MFShutdown()
        if initialised in (0, 1):
            ole32.CoUninitialize()
    return width, height, runs


def _run(reader, read, start, count, fps, width, height, flip, deadline):
    """Consecutive frames from ``start`` seconds, or from here if it is None."""
    if start is not None:
        position = PROPVARIANT()
        position.vt = VT_I8
        position.value = int(max(0.0, start) * 10_000_000)
        _method(reader, SR_SET_CURRENT_POSITION, ctypes.c_long,
                ctypes.POINTER(GUID), ctypes.POINTER(PROPVARIANT))(
            reader, ctypes.byref(GUID_NULL), ctypes.byref(position))

    interval = 1.0 / fps if fps > 0 else 0.0
    next_wanted = start or 0.0
    # The reader resumes at the keyframe before the requested time, so the
    # first frames arrive early and get skipped. Skipping is bounded: on a
    # long-GOP file, insisting on reaching the requested time burned the whole
    # read budget and returned nothing at all. Past the bound, the next frame
    # is taken wherever it lands.
    catch_up = MAX_CATCH_UP_READS
    blanks_left = MAX_BLANK_SKIPS
    reads = 0
    budget = count * MAX_READS_PER_FRAME
    out = []
    while len(out) < count and reads < budget:
        # A file that yields nothing must give up too, or it ties up the
        # worker that every other preview shares.
        if time.monotonic() > deadline:
            break
        sample = ctypes.c_void_p()
        flags = ctypes.c_ulong()
        stream = ctypes.c_ulong()
        stamp = ctypes.c_longlong()
        hr = read(reader, FIRST_VIDEO_STREAM, 0, ctypes.byref(stream),
                  ctypes.byref(flags), ctypes.byref(stamp),
                  ctypes.byref(sample))
        reads += 1
        if hr != 0 or (flags.value & ENDOFSTREAM):
            break
        if not sample:
            continue
        try:
            seconds = stamp.value / 10_000_000.0
            if seconds + 1e-6 < next_wanted and catch_up > 0:
                catch_up -= 1
                continue
            raw = _sample_bytes(sample)
            if raw is None:
                continue
            # Plenty of clips open on a black or white card, and a scene
            # change can land on one too. Opening a run there tells the viewer
            # nothing, so a few are passed over before taking what is there.
            if not out and blanks_left and _is_blank(raw):
                blanks_left -= 1
                next_wanted = seconds + interval
                continue
            next_wanted = seconds + interval
            out.append(_bgra_to_png(raw, width, height, flip))
        finally:
            _release(sample)
    return out


def _default_stride(media_type):
    """Signed row stride, or None. Negative means the rows arrive bottom-up."""
    value = ctypes.c_uint32()
    hr = _method(media_type, AT_GET_UINT32, ctypes.c_long,
                 ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_uint32))(
        media_type, ctypes.byref(MF_MT_DEFAULT_STRIDE), ctypes.byref(value))
    if hr != 0:
        return None
    return ctypes.c_int32(value.value).value


def _native_size(reader):
    """The clip's own pixel dimensions, or None."""
    native = ctypes.c_void_p()
    if _method(reader, SR_GET_NATIVE_MEDIA_TYPE, ctypes.c_long,
               ctypes.c_ulong, ctypes.c_ulong,
               ctypes.POINTER(ctypes.c_void_p))(
            reader, FIRST_VIDEO_STREAM, 0, ctypes.byref(native)) != 0:
        return None
    try:
        packed = ctypes.c_ulonglong()
        if _method(native, AT_GET_UINT64, ctypes.c_long,
                   ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_ulonglong))(
                native, ctypes.byref(MF_MT_FRAME_SIZE),
                ctypes.byref(packed)) != 0:
            return None
        width = int(packed.value >> 32)
        height = int(packed.value & 0xFFFFFFFF)
        return (width, height) if width > 0 and height > 0 else None
    finally:
        _release(native)


def _fit(native, box):
    """Largest even-sided box that keeps the source aspect ratio."""
    if not native:
        return (int(box[0]), int(box[1]))
    scale = min(box[0] / native[0], box[1] / native[1])
    width = max(2, int(native[0] * scale) & ~1)
    height = max(2, int(native[1] * scale) & ~1)
    return width, height


def _duration(reader):
    pv = PROPVARIANT()
    hr = _method(reader, SR_GET_PRESENTATION_ATTRIBUTE, ctypes.c_long,
                 ctypes.c_ulong, ctypes.POINTER(GUID),
                 ctypes.POINTER(PROPVARIANT))(
        reader, MEDIASOURCE, ctypes.byref(MF_PD_DURATION), ctypes.byref(pv))
    if hr != 0 or pv.value <= 0:
        return None
    return pv.value / 10_000_000.0


def _sample_bytes(sample):
    buffer = ctypes.c_void_p()
    if _method(sample, SAMPLE_CONVERT_CONTIGUOUS, ctypes.c_long,
               ctypes.POINTER(ctypes.c_void_p))(
            sample, ctypes.byref(buffer)) != 0 or not buffer:
        return None
    try:
        data = ctypes.POINTER(ctypes.c_ubyte)()
        length = ctypes.c_ulong()
        if _method(buffer, BUF_LOCK, ctypes.c_long,
                   ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                   ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))(
                buffer, ctypes.byref(data), None, ctypes.byref(length)) != 0:
            return None
        try:
            return bytes(bytearray(data[:length.value]))
        finally:
            _method(buffer, BUF_UNLOCK, ctypes.c_long)(buffer)
    finally:
        _release(buffer)
