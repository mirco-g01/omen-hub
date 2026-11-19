"""The webcam as an ambient light sensor, without getting in anybody's way.

The laptop has no ambient light sensor (no ISH, no ACPI0008, no IIO: checked
19/11/2026), so a single webcam frame stands in for one. Two rules make it
harmless to the other users of the camera (video calls, howdy on the IR node):

* **Claim first, touch later.** uvcvideo hands the streaming "privileges" to
  the first file handle that sets a format or asks for buffers; everybody
  else gets EBUSY until it closes. So the order here is S_FMT → REQBUFS →
  *then* the exposure controls → STREAMON. If anyone else holds the camera
  the claim fails and the controls (which any handle may change, even while
  another one streams) are never touched.
* **Leave at once.** :class:`OpenWatch` sees through inotify every open of
  the camera's nodes; :func:`measure` takes an ``abort_fd`` and drops the
  camera as soon as it becomes readable, so a call or a howdy prompt that
  starts during a shot finds the device free a few milliseconds later.

Everything is plain ioctl/mmap (no OpenCV): the capture must be abortable
mid-frame and the claim order must be exactly the one above.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import math
import mmap
import os
import select
import struct
import threading
import time
from ctypes import Structure, Union, c_int32, c_long, c_uint8, c_uint32, c_ulong, c_void_p, sizeof
from dataclasses import dataclass, field
from pathlib import Path


# -- V4L2 ABI (x86_64) -----------------------------------------------------------------

class _PixFormat(Structure):
    _fields_ = [(n, c_uint32) for n in ("width", "height", "pixelformat", "field", "bytesperline",
                                        "sizeimage", "colorspace", "priv", "flags", "ycbcr_enc",
                                        "quantization", "xfer_func")]


class _FmtUnion(Union):
    # v4l2_window holds pointers: the union is 8-byte aligned, hence _align
    _fields_ = [("pix", _PixFormat), ("raw", c_uint8 * 200), ("_align", c_void_p)]


class _Format(Structure):
    _fields_ = [("type", c_uint32), ("fmt", _FmtUnion)]


class _ReqBufs(Structure):
    _fields_ = [("count", c_uint32), ("type", c_uint32), ("memory", c_uint32),
                ("capabilities", c_uint32), ("flags", c_uint8), ("reserved", c_uint8 * 3)]


class _TimeVal(Structure):
    _fields_ = [("sec", c_long), ("usec", c_long)]


class _TimeCode(Structure):
    _fields_ = [("type", c_uint32), ("flags", c_uint32), ("frames", c_uint8), ("seconds", c_uint8),
                ("minutes", c_uint8), ("hours", c_uint8), ("userbits", c_uint8 * 4)]


class _BufM(Union):
    _fields_ = [("offset", c_uint32), ("userptr", c_ulong), ("planes", c_void_p), ("fd", c_int32)]


class _Buffer(Structure):
    _fields_ = [("index", c_uint32), ("type", c_uint32), ("bytesused", c_uint32), ("flags", c_uint32),
                ("field", c_uint32), ("timestamp", _TimeVal), ("timecode", _TimeCode),
                ("sequence", c_uint32), ("memory", c_uint32), ("m", _BufM), ("length", c_uint32),
                ("reserved2", c_uint32), ("request_fd", c_int32)]


class _Control(Structure):
    _fields_ = [("id", c_uint32), ("value", c_int32)]


class _QueryCtrl(Structure):
    _fields_ = [("id", c_uint32), ("type", c_uint32), ("name", c_uint8 * 32), ("minimum", c_int32),
                ("maximum", c_int32), ("step", c_int32), ("default_value", c_int32),
                ("flags", c_uint32), ("reserved", c_uint32 * 2)]


assert sizeof(_Format) == 208 and sizeof(_ReqBufs) == 20 and sizeof(_Buffer) == 88
assert sizeof(_QueryCtrl) == 68


def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


def _iow(nr, size):
    return (1 << 30) | (size << 16) | (ord("V") << 8) | nr


VIDIOC_G_FMT = _iowr(4, sizeof(_Format))
VIDIOC_S_FMT = _iowr(5, sizeof(_Format))
VIDIOC_REQBUFS = _iowr(8, sizeof(_ReqBufs))
VIDIOC_QUERYBUF = _iowr(9, sizeof(_Buffer))
VIDIOC_QBUF = _iowr(15, sizeof(_Buffer))
VIDIOC_DQBUF = _iowr(17, sizeof(_Buffer))
VIDIOC_STREAMON = _iow(18, 4)
VIDIOC_STREAMOFF = _iow(19, 4)
VIDIOC_G_CTRL = _iowr(27, sizeof(_Control))
VIDIOC_S_CTRL = _iowr(28, sizeof(_Control))
VIDIOC_QUERYCTRL = _iowr(36, sizeof(_QueryCtrl))

BUF_TYPE_CAPTURE = 1
MEMORY_MMAP = 1
YUYV = struct.unpack("<I", b"YUYV")[0]

CID_EXPOSURE_AUTO = 0x009A0901          # menu: 1 manual, 3 aperture priority
CID_EXPOSURE_ABSOLUTE = 0x009A0902      # units of 100 µs
CID_EXPOSURE_AUTO_PRIORITY = 0x009A0903  # "exposure_dynamic_framerate"
CID_GAIN = 0x00980913
EXPOSURE_MANUAL = 1
EXPOSURE_APERTURE_PRIORITY = 3


class Busy(Exception):
    """Someone else holds the camera: nothing was touched."""


class Aborted(Exception):
    """The shot was dropped because someone else opened the camera."""


class Unavailable(Exception):
    """No camera node (switched off, unplugged, no permission)."""


# -- the camera node ----------------------------------------------------------------------

DEFAULT_DEVICE = "/dev/video0"
WIDTH, HEIGHT = 640, 360


def find_device(watch: "OpenWatch | None" = None) -> str | None:
    """The RGB capture node: a capture-capable /dev/videoN offering YUYV.
    The IR node of the same module is GREY-only and is skipped."""
    base = Path("/sys/class/video4linux")
    nodes = sorted(base.glob("video*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 99)
    for p in nodes:
        dev = f"/dev/{p.name}"
        try:
            if _has_yuyv(dev, watch):
                return dev
        except OSError:
            continue
    return None


def _has_yuyv(dev: str, watch: "OpenWatch | None" = None) -> bool:
    VIDIOC_ENUM_FMT = _iowr(2, 64)
    if watch:
        watch.own_open()
    try:
        fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        if watch:
            watch.own_open_failed()
        raise
    try:
        for i in range(16):
            buf = bytearray(64)
            struct.pack_into("<II", buf, 0, i, BUF_TYPE_CAPTURE)
            try:
                fcntl.ioctl(fd, VIDIOC_ENUM_FMT, buf)
            except OSError:
                return False
            if struct.unpack_from("<I", buf, 44)[0] == YUYV:
                return True
        return False
    finally:
        os.close(fd)


def module_nodes(dev: str) -> list[str]:
    """Every /dev/videoN on the same USB device as `dev` (RGB + IR + their
    metadata nodes): an open of any of them means the module is in use."""
    try:
        mine = Path(f"/sys/class/video4linux/{Path(dev).name}/device").resolve().parent
    except OSError:
        return [dev]
    out = []
    for p in Path("/sys/class/video4linux").glob("video*"):
        try:
            if (p / "device").resolve().parent == mine:
                out.append(f"/dev/{p.name}")
        except OSError:
            continue
    return sorted(out) or [dev]


# -- one claimed session ------------------------------------------------------------------

@dataclass
class Frame:
    mean: float               # mean luma 0-255 (Y of YUYV)
    saturated: float          # fraction of pixels at >= 250
    exposure: int             # exposure_time_absolute used, 100 µs units
    t: float                  # seconds since the claim


@dataclass
class Reading:
    lum: float                # linearised luma per ms of exposure: the light estimate
    mean: float
    exposure_ms: float
    saturated: float
    frames: list[Frame] = field(default_factory=list)
    on_s: float = 0.0         # how long the camera stayed claimed (≈ LED on)
    first_s: float = 0.0
    next_exposure: int | None = None   # where the next measurement should start


class _Session:
    def __init__(self, dev: str, abort_fd: int | None, watch: "OpenWatch | None" = None):
        self.dev, self.abort_fd, self.watch = dev, abort_fd, watch
        self.fd = -1
        self.maps: list[mmap.mmap] = []
        self.saved: dict[int, int] = {}
        self.ranges: dict[int, tuple[int, int, int]] = {}
        self.streaming = False
        self.t0 = time.monotonic()
        self.found_fmt: _Format | None = None

    # claim ------------------------------------------------------------------------------
    def open(self):
        if self.watch:
            self.watch.own_open()
        try:
            self.fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if self.watch:
                self.watch.own_open_failed()
            if isinstance(exc, FileNotFoundError):
                raise Unavailable(f"{self.dev} non c'è (fotocamera spenta o scollegata?)") from exc
            if isinstance(exc, PermissionError):
                raise Unavailable(f"{self.dev}: permesso negato") from exc
            if exc.errno == errno.EBUSY:
                raise Busy("fotocamera occupata") from exc
            raise Unavailable(f"{self.dev}: {exc.strerror}") from exc
        if self.watch and self.watch.read():
            raise Aborted("fotocamera appena aperta da un'altra applicazione")

    def claim(self):
        self.open()
        found = _Format()
        found.type = BUF_TYPE_CAPTURE
        try:
            fcntl.ioctl(self.fd, VIDIOC_G_FMT, found)
        except OSError:
            found = None
        fmt = _Format()
        fmt.type = BUF_TYPE_CAPTURE
        fmt.fmt.pix.width, fmt.fmt.pix.height = WIDTH, HEIGHT
        fmt.fmt.pix.pixelformat = YUYV
        self._claim(VIDIOC_S_FMT, fmt)
        self.found_fmt = found               # ours now: put it back on release
        if fmt.fmt.pix.pixelformat != YUYV:
            raise Unavailable("la fotocamera non offre YUYV")
        self.w, self.h = fmt.fmt.pix.width, fmt.fmt.pix.height
        req = _ReqBufs(count=2, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
        self._claim(VIDIOC_REQBUFS, req)
        for i in range(req.count):
            b = _Buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, b)
            self.maps.append(mmap.mmap(self.fd, b.length, mmap.MAP_SHARED, mmap.PROT_READ,
                                       offset=b.m.offset))
        # only now, with the camera ours, read and change its controls
        for cid in (CID_EXPOSURE_AUTO, CID_EXPOSURE_ABSOLUTE, CID_GAIN, CID_EXPOSURE_AUTO_PRIORITY):
            q = _QueryCtrl(id=cid)
            try:
                fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, q)
                c = _Control(id=cid)
                fcntl.ioctl(self.fd, VIDIOC_G_CTRL, c)
            except OSError:
                continue
            self.ranges[cid] = (q.minimum, q.maximum, q.default_value)
            self.saved[cid] = c.value
        if CID_EXPOSURE_ABSOLUTE not in self.ranges:
            raise Unavailable("la fotocamera non ha l'esposizione manuale")
        # A camera found in manual mode was left so by a run that died: hand it
        # back in automatic mode, as the driver starts it.
        if self.saved.get(CID_EXPOSURE_AUTO) == EXPOSURE_MANUAL:
            self.saved[CID_EXPOSURE_AUTO] = EXPOSURE_APERTURE_PRIORITY

    def _claim(self, req, arg):
        try:
            fcntl.ioctl(self.fd, req, arg)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise Busy("fotocamera occupata") from exc
            raise

    def exposure_range(self) -> tuple[int, int]:
        lo, hi, _ = self.ranges[CID_EXPOSURE_ABSOLUTE]
        return lo, hi

    def _set(self, cid, value):
        if cid in self.ranges:
            try:
                fcntl.ioctl(self.fd, VIDIOC_S_CTRL, _Control(id=cid, value=int(value)))
            except OSError:
                pass

    # shots ------------------------------------------------------------------------------
    def shoot(self, exposure: int, gain: int | None = None, timeout: float = 3.0) -> Frame:
        """One frame at a fixed exposure. The stream is (re)started after the
        controls are set, so the very first frame already has them (measured:
        no warm-up frames needed on this camera)."""
        self._stream_off()
        lo, hi = self.exposure_range()
        exposure = max(lo, min(hi, int(exposure)))
        self._set(CID_EXPOSURE_AUTO, EXPOSURE_MANUAL)
        self._set(CID_EXPOSURE_AUTO_PRIORITY, 0)
        self._set(CID_EXPOSURE_ABSOLUTE, exposure)
        if gain is not None:
            self._set(CID_GAIN, gain)
        elif CID_GAIN in self.ranges:
            self._set(CID_GAIN, self.ranges[CID_GAIN][0])
        for i in range(len(self.maps)):
            fcntl.ioctl(self.fd, VIDIOC_QBUF, _Buffer(index=i, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(BUF_TYPE_CAPTURE))
        self.streaming = True
        deadline = time.monotonic() + timeout + exposure / 10000
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("nessun fotogramma dalla fotocamera")
            watch = [self.fd] + ([self.abort_fd] if self.abort_fd is not None else [])
            ready, _, _ = select.select(watch, [], [], left)
            if self.abort_fd is not None and self.abort_fd in ready:
                raise Aborted("fotocamera richiesta da un'altra applicazione")
            if self.fd not in ready:
                continue
            b = _Buffer(type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP)
            try:
                fcntl.ioctl(self.fd, VIDIOC_DQBUF, b)
            except BlockingIOError:
                continue
            data = self.maps[b.index][:b.bytesused]
            mean, sat = luma_stats(data, self.w, self.h)
            return Frame(mean, sat, exposure, time.monotonic() - self.t0)

    def _stream_off(self):
        if self.streaming:
            try:
                fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(BUF_TYPE_CAPTURE))
            except OSError:
                pass
            self.streaming = False

    def close(self):
        if self.fd < 0:
            return
        self._stream_off()
        # hand the controls back: values first (exposure is inactive in auto mode)
        for cid in (CID_EXPOSURE_ABSOLUTE, CID_GAIN):
            if cid in self.saved:
                self._set(cid, self.saved[cid])
        for cid in (CID_EXPOSURE_AUTO, CID_EXPOSURE_AUTO_PRIORITY):
            if cid in self.saved:
                self._set(cid, self.saved[cid])
        for m in self.maps:
            try:
                m.close()
            except BufferError:
                pass
        self.maps.clear()
        try:
            fcntl.ioctl(self.fd, VIDIOC_REQBUFS, _ReqBufs(count=0, type=BUF_TYPE_CAPTURE, memory=MEMORY_MMAP))
        except OSError:
            pass
        if self.found_fmt is not None:       # the format the next app finds is the one we found
            try:
                fcntl.ioctl(self.fd, VIDIOC_S_FMT, self.found_fmt)
            except OSError:
                pass
        os.close(self.fd)
        self.fd = -1


# Only the two side bands of the frame are measured. The middle half is where
# you sit, and you are what the screen and keyboard light up most (19/11, four
# photo series: room dark / lamp on × PC at 1 % / 100 %). Without you in it the
# error the screen adds with the lamp on fell from +34 % to +6 %, and the false
# light it shows in a dark room from 30 % to 15 % of the scale, with about the
# same signal. Weighting the dark zones instead failed: with the lamp behind
# you, you are one of the darkest zones and the most lit by the screen.
SIDE = 0.25            # each band: this share of the width


def luma_stats(data: bytes, w: int | None = None, h: int | None = None) -> tuple[float, float]:
    """Mean of Y (every other byte of YUYV) over the two side bands, and their
    share of clipped pixels. Without the frame size: the whole frame."""
    try:
        import numpy as np
        y = np.frombuffer(data, dtype=np.uint8)[0::2]
        if w and h and y.size >= w * h:
            img = y[:w * h].reshape(h, w)
            k = int(w * SIDE)
            y = np.concatenate((img[:, :k].ravel(), img[:, w - k:].ravel()))
        return float(y.mean()), float((y >= 250).mean())
    except ImportError:                       # slower, same numbers
        y = memoryview(data)[0::2]
        if w and h and len(y) >= w * h:
            k = int(w * SIDE)
            y = [v for r in range(h) for v in (list(y[r * w:r * w + k]) + list(y[r * w + w - k:(r + 1) * w]))]
        n = len(y) or 1
        return sum(y) / n, sum(1 for v in y if v >= 250) / n


# -- from pixels to light ------------------------------------------------------------------

# Luma is limited range: black sits at 16, not 0.
BLACK = 16.0
# Measured response of this camera (19/11/2026, one steady scene, gain 0):
# signal above black (mean - 16) against exposure in ms. The camera crushes
# the bottom (a toe below ~10) and compresses the top, so the exposure that
# a given signal "is worth" comes from this table, not from a gamma. On a
# second run under a different light the estimate stayed within ±3 % from
# 32 to 400 ms of exposure: reciprocity holds, one table serves every scene.
RESPONSE = ((0.06, 12.5), (0.48, 20.0), (1.76, 32.0), (4.45, 50.0), (6.8, 64.0),
            (9.63, 80.0), (13.2, 100.0), (17.52, 125.0), (23.37, 160.0), (29.64, 200.0),
            (37.0, 250.0), (46.02, 320.0), (55.41, 400.0), (66.32, 500.0), (80.41, 640.0),
            (94.39, 800.0))
# Above the table: the slope of its last stretch (log-log, 400 → 800 ms).
_TOP_SLOPE = math.log(RESPONSE[-1][0] / RESPONSE[-4][0]) / math.log(RESPONSE[-1][1] / RESPONSE[-4][1])
# A frame is usable with a signal (mean - black) in [V_LOW, V_HIGH]: in one
# scene a 100 ms frame with signal 1.3 gave the same light as a 250 ms one
# with signal 8 (ratio 1.00 ± 4 %, 19/11); at 0.16 it was 66 % off. A retry
# aims at V_AIM.
V_LOW, V_HIGH, V_AIM = 1.0, 90.0, 3.0
# The next shot aims low (NEXT_AIM, twice the minimum: a room may halve its
# light before a retry is needed) because streaming time is energy: the first
# frame comes ~0.21 s + 3 exposures after STREAMON and STREAMOFF waits for
# the frame in progress (0.97 s on at 250 ms, 0.52 s at 100 ms, 0.33 s at
# 33 ms), and a shot's energy grows with it (RAPL + battery, 19/11). Below one
# frame period (33 ms at 30 fps) nothing is saved any more, so that is the
# floor; NEXT_MAX keeps a bright scene well away from clipping.
NEXT_AIM, NEXT_MAX = 2.0, 60.0
FRAME_FLOOR = 330          # 100 µs units


def linearise(v: float) -> float:
    """Signal above black → equivalent exposure (ms) at the reference light."""
    if v <= RESPONSE[0][0]:
        return RESPONSE[0][1] * max(v, 0.005) / RESPONSE[0][0]
    for (v0, e0), (v1, e1) in zip(RESPONSE, RESPONSE[1:]):
        if v <= v1:
            t = (math.log(v) - math.log(v0)) / (math.log(v1) - math.log(v0))
            return math.exp(math.log(e0) + t * (math.log(e1) - math.log(e0)))
    v1, e1 = RESPONSE[-1]
    return e1 * (v / v1) ** (1 / _TOP_SLOPE)


def light(mean: float, exposure_ms: float) -> float:
    """The light estimate: 1.0 is the scene the table was measured in (a dim
    room on a November afternoon)."""
    return linearise(max(mean - BLACK, 0.0)) / max(exposure_ms, 0.01)


def exposure_for(f: Frame, v_target: float) -> float:
    """The exposure that would give signal `v_target` in the scene of `f`."""
    return f.exposure * linearise(v_target) / linearise(max(f.mean - BLACK, 0.05))


def next_exposure(f: Frame, lo: int, hi: int) -> int | None:
    """The exposure (100 µs units) for another shot now, or None if this
    frame is usable or the range is exhausted."""
    v = f.mean - BLACK
    if V_LOW <= v <= V_HIGH:
        return None
    if v > V_HIGH:
        if f.exposure <= lo:
            return None
        # clipped pixels make the mean lie low: step down harder
        e = exposure_for(f, V_AIM) * (0.5 if f.saturated > 0.01 else 1.0)
        e = min(e, f.exposure * 0.5)
    else:
        if f.exposure >= hi:
            return None
        e = min(exposure_for(f, V_AIM), f.exposure * 16)
    e = max(lo, min(hi, int(e)))
    return None if e == f.exposure else e


def plan_exposure(f: Frame, lo: int, hi: int) -> int:
    """The exposure to start the *next* measurement with: the shortest that
    still gives a reliable signal, never below one frame period."""
    if f.mean - BLACK < 0.3:
        return hi                            # black frame: nothing to go on
    e = min(max(exposure_for(f, NEXT_AIM), FRAME_FLOOR), exposure_for(f, NEXT_MAX))
    return max(lo, min(hi, int(e)))


def measure(dev: str, max_exposure_ms: float, start_exposure: int | None = None,
            abort_fd: int | None = None, max_shots: int = 4,
            watch: "OpenWatch | None" = None, cap_exposure: int | None = None) -> Reading:
    """Claim the camera, take one frame, steer the exposure until the signal
    is neither crushed nor clipped (or the range ends), release.
    `start_exposure` (100 µs units) is the one that worked last time, so a
    steady scene costs a single frame. `cap_exposure` limits this shot (the
    one-frame check in the dark); the next exposure is still planned against
    `max_exposure_ms`."""
    s = _Session(dev, abort_fd, watch)
    frames: list[Frame] = []
    try:
        s.claim()
        lo, hi = s.exposure_range()
        hi = max(lo, min(hi, int(round(max_exposure_ms * 10))))
        top = max(lo, min(hi, cap_exposure)) if cap_exposure else hi
        e = max(lo, min(top, start_exposure or hi))
        for _ in range(max_shots):
            f = s.shoot(e)
            frames.append(f)
            e = next_exposure(f, lo, top)
            if e is None:
                break
    finally:
        on = time.monotonic() - s.t0
        s.close()
    best = _best(frames)
    return Reading(lum=light(best.mean, best.exposure / 10.0), mean=best.mean,
                   exposure_ms=best.exposure / 10.0, saturated=best.saturated, frames=frames,
                   on_s=on, first_s=frames[0].t if frames else 0.0,
                   next_exposure=plan_exposure(best, lo, hi))


def sample(dev: str, seconds: float, max_exposure_ms: float, on_value=None, stop=None,
           wait_busy: float = 4.0) -> list[float]:
    """Hold the camera for `seconds` and return one light estimate per usable
    frame (calibration: the torch on the lens, or the darkest room). The
    exposure is steered frame by frame as in measure(). A camera busy at the
    start is retried for `wait_busy` seconds: the service drops its own shot
    as soon as it sees us open the node."""
    s = _Session(dev, None)
    t_wait = time.monotonic() + wait_busy
    while True:
        try:
            s.claim()
            break
        except Busy:
            s.close()
            if time.monotonic() > t_wait:
                raise
            time.sleep(0.2)
            s = _Session(dev, None)
    values: list[float] = []
    try:
        lo, hi = s.exposure_range()
        hi = max(lo, min(hi, int(round(max_exposure_ms * 10))))
        e = hi
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (stop and stop()):
            f = s.shoot(e)
            nxt = next_exposure(f, lo, hi)
            if nxt is None:                 # in range, or as far as the camera goes
                lum = light(f.mean, f.exposure / 10.0)
                values.append(lum)
                if on_value:
                    on_value(lum, f)
            else:
                e = nxt
    finally:
        s.close()
    return values


def _best(frames: list[Frame]) -> Frame:
    """The frame most inside the usable range (last one wins ties)."""
    def score(f):
        v = f.mean - BLACK
        if v > V_HIGH:
            return (v - V_HIGH) + 100 * f.saturated
        if v < V_LOW:
            return (V_LOW - v) * 4
        return 0.0
    return min(reversed(frames), key=score)


def ambient(lum: float, dark: float, bright: float) -> float:
    """Light estimate → 0-100 between the calibrated dark and bright ends, on
    a log scale (the eye works in ratios, and the range spans decades)."""
    lo, hi = math.log10(max(dark, 1e-6)), math.log10(max(bright, 1e-6))
    if hi - lo < 0.05:
        hi = lo + 0.05
    x = (math.log10(max(lum, 1e-6)) - lo) / (hi - lo) * 100
    return max(0.0, min(100.0, x))


# -- who else is using the camera ---------------------------------------------------------

IN_OPEN = 0x020
IN_IGNORED = 0x8000
_EVENT = struct.Struct("iIII")


class OpenWatch:
    """inotify (IN_OPEN) on every node of the camera module. Any process is
    seen, root ones such as howdy included: inotify needs access to the node,
    not to the process.

    It answers one question: "has a stranger opened the camera since I last
    looked?". It does not count handles: inotify merges identical events
    that are still queued, so an open/close tally drifts. Who is using the
    camera *now* comes from :func:`busy_now` instead.

    Our own opens are announced (:meth:`own_open`) and the claim drains the
    queue right after its open, so our event is booked before a stranger's
    can be queued next to it (and merged with it).
    """

    def __init__(self, nodes: list[str]):
        libc = ctypes.CDLL(None, use_errno=True)
        self._add = libc.inotify_add_watch
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self._lock = threading.Lock()
        self.nodes: dict[int, str] = {}
        self._own = 0                 # announced opens whose event is not seen yet
        self.last_other_open = 0.0    # monotonic
        self.rewatch(nodes)

    def rewatch(self, nodes: list[str]):
        """(Re)arm the watches: a node that went away (camera switched off)
        and came back is a new inode."""
        for n in nodes:
            if n in self.nodes.values():
                continue
            wd = self._add(self.fd, n.encode(), IN_OPEN)
            if wd >= 0:
                self.nodes[wd] = n

    def own_open(self):
        with self._lock:
            self._own += 1

    def own_open_failed(self):
        with self._lock:
            self._own = max(0, self._own - 1)

    def read(self) -> bool:
        """Drain the queue; True if a stranger opened a node meanwhile."""
        stranger = False
        with self._lock:
            while True:
                try:
                    data = os.read(self.fd, 64 * _EVENT.size)
                except BlockingIOError:
                    break
                for off in range(0, len(data), _EVENT.size):
                    wd, mask, _cookie, _len = _EVENT.unpack_from(data, off)
                    if mask & IN_IGNORED:
                        self.nodes.pop(wd, None)
                    elif mask & IN_OPEN:
                        if self._own:
                            self._own -= 1
                        else:
                            stranger = True
                            self.last_other_open = time.monotonic()
        return stranger

    def close(self):
        os.close(self.fd)


def holders(nodes: list[str]) -> int:
    """Handles on the camera nodes held by other processes of this user (the
    /proc of root processes is not readable: see streaming())."""
    count, me = 0, str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(f"/proc/{pid}/fd/{fd}") in nodes:
                    count += 1
            except OSError:
                pass
    return count


def streaming(dev: str) -> bool | None:
    """True while anybody streams from any node of the module (and for the
    2 s autosuspend delay after): uvcvideo keeps the USB device awake only
    while a stream runs. Root processes included. None if USB autosuspend is
    off for the camera, which makes the device look always awake."""
    try:
        usb = Path(f"/sys/class/video4linux/{Path(dev).name}/device").resolve().parent / "power"
        if (usb / "control").read_text().strip() != "auto":
            return None
        return (usb / "runtime_status").read_text().strip() in ("active", "resuming")
    except OSError:
        return None
