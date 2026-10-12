"""Edits to /etc/omen-fan-control/config.json, with the same invariants as
omen_logic.FanController (fork mirco-g01/omen-fan-control):

- "curves" is the named library, "active_curve" points into it, and the legacy
  single "curve" key is kept equal to the active curve;
- the running daemon reloads the file when its mtime changes, so a write is
  all it takes (no restart).
"""

from __future__ import annotations

import json

MODES = ("curve", "auto", "max", "manual")
TEMP_SOURCES = ("package", "core_mean", "hybrid")
INTERPOLATIONS = ("smooth", "discrete")
CLEANER_DURATION_RANGE = (10, 90)   # the EC drops user fan control after 120 s
CLEANER_SPEED_RANGE = (10, 60)      # x100 RPM
CLEANER_MAX_TEMP = 75


def _int(v, lo, hi, what):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: valore non numerico") from None
    if not lo <= v <= hi:
        raise ValueError(f"{what}: {v} fuori da {lo}–{hi}")
    return v


def validate_points(points) -> list[list[float]]:
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("una curva richiede almeno 2 punti")
    out = []
    for pt in points:
        try:
            t, p = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError):
            raise ValueError("punto curva non valido") from None
        if not (0 <= t <= 110 and 0 <= p <= 100):
            raise ValueError(f"punto fuori scala: {t:.0f} °C / {p:.0f} %")
        out.append([round(t, 2), round(p, 2)])
    out.sort(key=lambda x: x[0])
    return out


def validate_curve_name(name) -> str:
    name = str(name or "").strip()
    if not name or len(name) > 40 or any(c in name for c in "/\\\n\t"):
        raise ValueError("nome curva non valido")
    return name


def _sync_curve(cfg: dict):
    curves = cfg.get("curves") or {}
    if curves:
        if cfg.get("active_curve") not in curves:
            cfg["active_curve"] = next(iter(curves))
        cfg["curve"] = curves[cfg["active_curve"]]


def apply_changes(cfg: dict, changes: dict) -> dict:
    """Return a new config with ``changes`` applied. Raises ValueError."""
    cfg = json.loads(json.dumps(cfg))
    curves = cfg.setdefault("curves", {})
    if not curves and cfg.get("curve"):
        curves["Default"] = cfg["curve"]

    for key, val in changes.items():
        if key == "mode":
            if val not in MODES:
                raise ValueError(f"modalità ventole non valida: {val}")
            cfg["mode"] = val
        elif key == "manual_pwm":
            cfg["manual_pwm"] = _int(val, 0, 255, "PWM manuale")
        elif key == "temp_source":
            if val not in TEMP_SOURCES:
                raise ValueError(f"sorgente temperatura non valida: {val}")
            cfg["temp_source"] = val
        elif key == "spike_window":
            cfg["spike_window"] = _int(val, 1, 120, "spike window")
        elif key == "ma_window":
            cfg["ma_window"] = _int(val, 1, 120, "media mobile")
        elif key == "curve_interpolation":
            if val not in INTERPOLATIONS:
                raise ValueError("interpolazione non valida")
            cfg["curve_interpolation"] = val
        elif key == "active_curve":
            name = validate_curve_name(val)
            if name not in curves:
                raise ValueError(f"curva inesistente: {name}")
            cfg["active_curve"] = name
        elif key == "save_curve":
            name = validate_curve_name(val.get("name"))
            curves[name] = validate_points(val.get("points"))
            if val.get("activate"):
                cfg["active_curve"] = name
        elif key == "delete_curve":
            name = validate_curve_name(val)
            if name not in curves:
                raise ValueError(f"curva inesistente: {name}")
            if len(curves) == 1:
                raise ValueError("non si può eliminare l'unica curva")
            del curves[name]
        elif key == "rename_curve":
            old, new = validate_curve_name(val.get("old")), validate_curve_name(val.get("new"))
            if old not in curves:
                raise ValueError(f"curva inesistente: {old}")
            if new != old and new in curves:
                raise ValueError(f"esiste già una curva '{new}'")
            cfg["curves"] = curves = {(new if k == old else k): v for k, v in curves.items()}
            if cfg.get("active_curve") == old:
                cfg["active_curve"] = new
        elif key == "cleaner_auto":
            cfg["cleaner_auto"] = bool(val)
        elif key == "cleaner_interval_hours":
            try:
                h = float(val)
            except (TypeError, ValueError):
                raise ValueError("intervallo pulizia non numerico") from None
            if not 1 <= h <= 24 * 90:
                raise ValueError("intervallo pulizia fuori scala (1 h – 90 giorni)")
            cfg["cleaner_interval_hours"] = h
        elif key == "cleaner_window":
            if val is None:
                cfg["cleaner_window_start"], cfg["cleaner_window_end"] = 0, 24
            else:
                a, b = _int(val[0], 0, 24, "finestra inizio"), _int(val[1], 0, 24, "finestra fine")
                cfg["cleaner_window_start"], cfg["cleaner_window_end"] = a, b
        elif key == "cleaner_duration":
            cfg["cleaner_duration"] = _int(val, *CLEANER_DURATION_RANGE, "durata pulizia")
        elif key == "cleaner_speed":
            cfg["cleaner_speed"] = _int(val, *CLEANER_SPEED_RANGE, "velocità pulizia")
        else:
            raise ValueError(f"opzione ventole non gestita: {key}")
    _sync_curve(cfg)
    return cfg


def curve_value(points: list, temp: float, smooth: bool = True) -> float:
    """Fan % for ``temp`` on a curve (for the GUI marker)."""
    if not points:
        return 0.0
    pts = sorted(points)
    if temp <= pts[0][0]:
        return pts[0][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            if not smooth or t1 == t0:
                return p0
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]
