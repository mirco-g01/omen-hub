"""Adaptive brightness: settings, curves and the arithmetic of the service.

The service (omen_hub/adaptived.py, user unit omen-hub-luce.service) reads the
room light from the webcam (omen_hub/camlight.py) every `interval_s` seconds
and sets screen and keyboard brightness through powerdevil from two curves,
"room light 0-100 % → brightness %". The GUI edits the settings file and
reads the status file; nothing here needs Qt or D-Bus, so it is all testable.

Room light 0 % and 100 % are the two ends of the calibration: the darkest
scene you use the laptop in and a phone torch on the lens. In between the
scale is logarithmic (see camlight.ambient).
"""

from __future__ import annotations

import copy
import json
import math
import os
import time
from pathlib import Path

UNIT = "omen-hub-luce.service"
# source_client_context of every brightness change we make: powerdevil echoes
# it in BrightnessChanged, so our own changes are told apart from the user's.
CONTEXT = "omen-hub-luce"

_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
CONFIG = Path(os.environ.get("OMEN_HUB_LUCE_CONFIG") or _CONFIG_HOME / "omen-hub" / "luce-adattiva.json")
STATUS = Path(os.environ.get("OMEN_HUB_LUCE_STATUS") or _RUNTIME / "omen-hub" / "luce-adattiva.json")
# Pauses survive a restart of the service (not a logout); requests come from
# `omen-hub --luce` (the tray switch) with SIGUSR2.
PAUSES = STATUS.with_name("luce-adattiva-pausa.json")
REQUEST = STATUS.with_name("luce-adattiva-richiesta.json")

OUTPUTS = ("screen", "keyboard")
WHEN = ("battery", "always")
PROFILES = ("battery", "ac")              # one curve per power source
NAMES = {"screen": "schermo", "keyboard": "tastiera"}

INTERVAL_RANGE = (10, 900)        # s
EXPOSURE_RANGE = (5, 1000)        # ms: the camera tops out at 1 s
SCREEN_MIN = 1.0                  # % : the panel is never sent to black

TRANSITION_RANGE = (0.0, 10.0)   # s: how long a change of level takes
DELTA_RANGE = (0, 50)            # room-light points (0-100 scale) before a level moves
RAMP_STEP_S = 0.05               # one brightness write every 50 ms during a transition
PAUSE_RANGE = (0.25, 24.0)       # h: a manual change pauses that output this long...
RESUME_RANGE = (5, 100)          # ...or until the room light moves this many points (100 = never)
# Keyboard: only while you type. Away longer than this, powerdevil may have
# dimmed it, and lighting a keyboard nobody uses is pointless anyway.
KBD_ACTIVE_S = 60
# The part of the frame the light is measured on (camlight.SIDE): a
# calibration made on another part is a different scale.
CAL_ZONE = "lati-25"
# A phone torch on the lens clips even at the shortest exposure (>1000 in
# camlight.light() units); a lit room is ~1, daylight ~10-100. A "bright end"
# below this never saw the torch and is not kept.
TORCH_MIN = 20.0
# A stranger opened the camera this recently: wait (it may be about to stream).
STRANGER_GRACE_S = 10
# No photos at all once you have been away this long.
AWAY_S = 600
# In the dark a long exposure is needed to tell "dim" from "dark", and the
# camera LED stays on ~3 exposures. So a long shot only when the room turns
# dark and then every DARK_REFRESH_S; in between one-frame shots (LED ~0.25 s)
# only check that it is still dark, and a light coming on is seen at once.
DARK_REFRESH_S = 600
# A shot counts as "long" beyond two frame periods (100 µs units): up to there
# the LED stays on ≤ ~0.35 s, no reason to save on it.
LONG_FROM = 660
# A brightness change this soon after plugging/unplugging is powerdevil
# loading the power profile, not you.
PROFILE_LOAD_S = 6.0

_SCREEN = [[0, 10], [30, 30], [50, 50], [70, 80], [85, 100]]
_KEYBOARD = [[0, 60], [25, 50], [40, 20], [55, 0]]
DEFAULTS = {
    # the two curves start equal; "Solo a batteria" only ever uses "battery"
    "screen": {"enabled": True, "when": "always",
               "curves": {"battery": _SCREEN, "ac": [p[:] for p in _SCREEN]}},
    "keyboard": {"enabled": True, "when": "always",
                 "curves": {"battery": _KEYBOARD, "ac": [p[:] for p in _KEYBOARD]}},
    "interval_s": 30,
    "max_exposure_ms": 250,
    "transition_s": 2.0,
    # the level only moves once the room light is this many points away from
    # the measurement that set the current level (not from the previous one:
    # a slow drift adds up and does get through)
    "min_delta": 5,
    # a level you set by hand (keys, slider, the tray switch) stops the
    # automatic control of that output until the timer runs out or the room
    # light changes a lot
    "pause_hours": 3.0,
    "resume_delta": 30,
    # camlight.light() units: 1.0 = a dim room on a November afternoon.
    # Guesses until the user measures both ends. `zone` is the part of the
    # frame they were measured on: values taken on another part do not count.
    "calibration": {"dark": 0.05, "bright": 200.0, "dark_at": None, "bright_at": None,
                    "zone": CAL_ZONE},
}


# -- settings -------------------------------------------------------------------------------

def _num(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    return max(lo, min(hi, f))


def clean_curve(points, default) -> list[list[float]]:
    """At least two points, x strictly increasing, both axes in 0..100."""
    out: dict[float, float] = {}
    try:
        for x, y in points:
            x, y = _num(x, 0, 100, None), _num(y, 0, 100, None)
            if x is not None and y is not None:
                out[round(x, 1)] = round(y, 1)
    except (TypeError, ValueError):
        return copy.deepcopy(default)
    pts = [[x, out[x]] for x in sorted(out)]
    return pts if len(pts) >= 2 else copy.deepcopy(default)


def validate(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    cfg = copy.deepcopy(DEFAULTS)
    for out in OUTPUTS:
        src = raw.get(out) if isinstance(raw.get(out), dict) else {}
        dst = cfg[out]
        if "enabled" in src:
            dst["enabled"] = bool(src["enabled"])
        if src.get("when") in WHEN:
            dst["when"] = src["when"]
        curves = src.get("curves") if isinstance(src.get("curves"), dict) else {}
        for prof in PROFILES:
            if prof in curves:
                dst["curves"][prof] = clean_curve(curves[prof], DEFAULTS[out]["curves"][prof])
            elif "curve" in src:             # settings from before the per-source curves
                dst["curves"][prof] = clean_curve(src["curve"], DEFAULTS[out]["curves"][prof])
    cfg["interval_s"] = int(_num(raw.get("interval_s"), *INTERVAL_RANGE, DEFAULTS["interval_s"]))
    cfg["max_exposure_ms"] = int(_num(raw.get("max_exposure_ms"), *EXPOSURE_RANGE,
                                      DEFAULTS["max_exposure_ms"]))
    cfg["transition_s"] = round(_num(raw.get("transition_s"), *TRANSITION_RANGE,
                                     DEFAULTS["transition_s"]), 1)
    cfg["min_delta"] = int(_num(raw.get("min_delta"), *DELTA_RANGE, DEFAULTS["min_delta"]))
    cfg["pause_hours"] = round(_num(raw.get("pause_hours"), *PAUSE_RANGE, DEFAULTS["pause_hours"]), 2)
    cfg["resume_delta"] = int(_num(raw.get("resume_delta"), *RESUME_RANGE, DEFAULTS["resume_delta"]))
    cal = raw.get("calibration") if isinstance(raw.get("calibration"), dict) else {}
    if cal.get("zone") != CAL_ZONE:
        cal = {}                             # measured on the whole frame: another scale
    dst = cfg["calibration"]
    for end in ("dark", "bright"):
        v = _num(cal.get(end), 1e-6, 1e9, None)
        if v is None or (end == "bright" and v < TORCH_MIN):
            continue                         # missing, or a torch that never reached the lens
        dst[end] = v
        if isinstance(cal.get(f"{end}_at"), (int, float)):
            dst[f"{end}_at"] = cal[f"{end}_at"]
    if dst["bright"] <= dst["dark"] * 1.5:           # an unusable pair: back to the guesses
        dst.update(copy.deepcopy(DEFAULTS["calibration"]))
    return cfg


def load(path: Path = CONFIG) -> dict:
    try:
        return validate(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return validate(None)


def _atomic_write(path: Path, data: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def save(cfg: dict, path: Path = CONFIG) -> dict:
    clean = validate(cfg)
    _atomic_write(path, clean)
    return clean


# -- curves and decisions ----------------------------------------------------------------------

def curve_value(curve, x: float) -> float:
    """Straight lines between the points, flat before the first and after the last."""
    if x <= curve[0][0]:
        return float(curve[0][1])
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(curve[-1][1])


def floor_of(out: str) -> float:
    return SCREEN_MIN if out == "screen" else 0.0


def profile_of(on_battery: bool) -> str:
    return "battery" if on_battery else "ac"


def curve_of(cfg: dict, out: str, profile: str):
    return cfg[out]["curves"][profile]


def target(cfg: dict, out: str, x: float, profile: str) -> float:
    """Brightness % for room light `x` on the curve of `profile`."""
    return max(floor_of(out), min(100.0, curve_value(curve_of(cfg, out, profile), x)))


def is_active(cfg: dict, out: str, on_battery: bool) -> bool:
    o = cfg[out]
    return bool(o["enabled"]) and (o["when"] == "always" or on_battery)


def light_moved(x: float, anchor: float | None, min_delta: float) -> bool:
    """Has the room light moved far enough from `anchor`, the measurement
    that set the current level, for the level to be reconsidered?"""
    return anchor is None or abs(x - anchor) >= min_delta


def level_differs(want: float, current: float | None) -> bool:
    return current is None or abs(want - current) >= 0.5


def ramp(start: float, end: float, duration: float, quantum: float = 0.1) -> tuple[list[float], float]:
    """A change spread over `duration` seconds: the values to write and the
    pause between them. One write every RAMP_STEP_S, but never steps smaller
    than `quantum` (the keyboard only knows whole percents)."""
    if duration <= 0 or abs(end - start) < quantum:
        return [end], 0.0
    n = max(1, min(int(round(duration / RAMP_STEP_S)), int(abs(end - start) / quantum)))
    return [start + (end - start) * (i + 1) / n for i in range(n)], duration / n


class Pauses:
    """Outputs whose automatic control you suspended, by hand or from the
    tray. Each one resumes when `pause_hours` have passed or when the room
    light is `resume_delta` points away from where it was at the pause (if the
    room had not been measured yet, the first measurement after is the start)."""

    def __init__(self, data: dict | None = None):
        self.d: dict[str, dict | None] = {o: None for o in OUTPUTS}
        for o in OUTPUTS:
            p = (data or {}).get(o)
            if isinstance(p, dict) and isinstance(p.get("since"), (int, float)):
                self.d[o] = {"since": float(p["since"]), "why": str(p.get("why") or ""),
                             "x": p["x"] if isinstance(p.get("x"), (int, float)) else None}

    def paused(self, out: str) -> bool:
        return self.d[out] is not None

    def pause(self, out: str, now: float, x: float | None, why: str):
        self.d[out] = {"since": now, "x": x, "why": why}

    def resume(self, out: str):
        self.d[out] = None

    def until(self, out: str, hours: float) -> float | None:
        p = self.d[out]
        return None if p is None else p["since"] + hours * 3600

    def due(self, out: str, now: float, x: float | None, hours: float, delta: float) -> str | None:
        """Why the pause of `out` is over, or None."""
        p = self.d[out]
        if p is None:
            return None
        if now >= p["since"] + hours * 3600:
            return "timer"
        if x is None:
            return None
        if p["x"] is None:
            p["x"] = x                       # first measurement after the pause: the start
            return None
        if delta < RESUME_RANGE[1] and abs(x - p["x"]) >= delta:
            return f"luce cambiata di {abs(x - p['x']):.0f} punti"
        return None

    def to_json(self) -> dict:
        return {o: p for o, p in self.d.items() if p is not None}

    @classmethod
    def load(cls, path: Path = PAUSES) -> "Pauses":
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path = PAUSES):
        try:
            _atomic_write(path, self.to_json())
        except OSError:
            pass


def read_request(path: Path = REQUEST) -> dict | None:
    """The tray's last request, consumed (deleted) on read."""
    try:
        data = json.loads(Path(path).read_text())
        Path(path).unlink()
    except (OSError, ValueError):
        return None
    if data.get("azione") not in ("pausa", "riprendi"):
        return None
    outs = [o for o in data.get("uscite") or OUTPUTS if o in OUTPUTS]
    return {"azione": data["azione"], "uscite": outs}


def write_request(action: str, outputs: list[str], path: Path = REQUEST):
    _atomic_write(path, {"azione": action, "uscite": outputs, "t": time.time()})


class DarkMemory:
    """The last long shot taken in the dark, and whether it still stands."""

    def __init__(self):
        self.lum: float | None = None
        self.at: float | None = None          # time.time() of the long shot

    def short_only(self, now: float, planned: int | None, long_from: int = LONG_FROM) -> bool:
        """Cap this shot at one frame period? Only while it has stayed dark
        since a long shot taken less than DARK_REFRESH_S ago."""
        return (self.lum is not None and planned is not None and planned > long_from
                and now - self.at < DARK_REFRESH_S)

    def settle(self, now: float, capped: bool, too_dark: bool, lum: float,
               exposure: int, long_from: int = LONG_FROM) -> tuple[float, str]:
        """The light to use for this shot and where it comes from."""
        if capped and too_dark:
            return self.lum, "buio confermato"
        if not capped and exposure > long_from:
            self.lum, self.at = lum, now       # a long shot: the dark reference
            return lum, "lunga"
        self.lum = self.at = None              # light enough for one frame: the memory is over
        return lum, "breve"


# -- status (written by the service, read by the GUI) ---------------------------------------

def write_status(data: dict, path: Path = STATUS):
    data = dict(data, time=time.time(), pid=os.getpid())
    try:
        _atomic_write(path, data)
    except OSError:
        pass


def read_status(path: Path = STATUS) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
