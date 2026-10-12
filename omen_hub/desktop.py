"""Things the GUI does as the logged-in user, without omen-hubd: brightness
through powerdevil, powerdevil's per-profile settings, user units, journals
and the omen-fx control socket."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time

from . import paths, protocol

PD_SERVICE = "org.kde.Solid.PowerManagement"
PD_DISPLAY = ("/org/kde/Solid/PowerManagement/Actions/BrightnessControl",
              "org.kde.Solid.PowerManagement.Actions.BrightnessControl")
PD_KBD = ("/org/kde/Solid/PowerManagement/Actions/KeyboardBrightnessControl",
          "org.kde.Solid.PowerManagement.Actions.KeyboardBrightnessControl")


def _dbus_call(obj_iface, method, *args):
    from PyQt6.QtDBus import QDBusConnection, QDBusInterface
    iface = QDBusInterface(PD_SERVICE, obj_iface[0], obj_iface[1], QDBusConnection.sessionBus())
    if not iface.isValid():
        raise RuntimeError("powerdevil non raggiungibile su D-Bus")
    reply = iface.call(method, *args)
    err = reply.errorMessage()
    if err:
        raise RuntimeError(err)
    vals = reply.arguments()
    return vals[0] if vals else None


def brightness() -> tuple[int, int] | None:
    """(value, max) for the panel, through powerdevil (which omen-backlight-sync
    mirrors to the EC register, so it is the real backlight)."""
    try:
        return int(_dbus_call(PD_DISPLAY, "brightness")), int(_dbus_call(PD_DISPLAY, "brightnessMax"))
    except Exception:
        return None


def set_brightness(value: int):
    _dbus_call(PD_DISPLAY, "setBrightness", int(value))


def kbd_brightness() -> tuple[int, int] | None:
    try:
        return int(_dbus_call(PD_KBD, "keyboardBrightness")), int(_dbus_call(PD_KBD, "keyboardBrightnessMax"))
    except Exception:
        return None


def set_kbd_brightness(value: int):
    _dbus_call(PD_KBD, "setKeyboardBrightness", int(value))


# -- powerdevil per-profile brightness -------------------------------------------

PD_PROFILES = (("AC", "Rete"), ("Battery", "Batteria"), ("LowBattery", "Batteria scarica"))


def _kread(group: list[str], key: str, default: str = "") -> str:
    exe = shutil.which("kreadconfig6")
    if not exe:
        return default
    cmd = [exe, "--file", "powerdevilrc"]
    for g in group:
        cmd += ["--group", g]
    cmd += ["--key", key, "--default", default]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return default


def _kwrite(group: list[str], key: str, value: str):
    cmd = ["kwriteconfig6", "--file", "powerdevilrc"]
    for g in group:
        cmd += ["--group", g]
    cmd += ["--key", key, value]
    subprocess.run(cmd, check=True, timeout=3)


def powerdevil_profiles() -> dict:
    out = {}
    for prof, _ in PD_PROFILES:
        out[prof] = {
            "display_on": _kread([prof, "Display"], "UseProfileSpecificDisplayBrightness", "false") == "true",
            "display": int(_kread([prof, "Display"], "DisplayBrightness", "50") or 50),
            "kbd_on": _kread([prof, "Keyboard"], "UseProfileSpecificKeyboardBrightness", "false") == "true",
            "kbd": int(_kread([prof, "Keyboard"], "KeyboardBrightness", "50") or 50),
        }
    return out


def save_powerdevil_profiles(values: dict):
    """Write the per-profile levels; powerdevil only applies them on a profile
    change, so it is restarted afterwards (verified 18/09)."""
    rc = paths.POWERDEVILRC
    if rc.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(rc, rc.with_name(f"powerdevilrc.bak-omen-hub-{stamp}"))
    for prof, v in values.items():
        _kwrite([prof, "Display"], "UseProfileSpecificDisplayBrightness", "true" if v["display_on"] else "false")
        _kwrite([prof, "Display"], "DisplayBrightness", str(int(v["display"])))
        _kwrite([prof, "Keyboard"], "UseProfileSpecificKeyboardBrightness", "true" if v["kbd_on"] else "false")
        _kwrite([prof, "Keyboard"], "KeyboardBrightness", str(int(v["kbd"])))
    subprocess.run(["systemctl", "--user", "restart", "plasma-powerdevil.service"], check=True, timeout=20)


# -- user units / journal ------------------------------------------------------------

USER_ACTIONS = {"start": ["start"], "stop": ["stop"], "restart": ["restart"],
                "enable": ["enable", "--now"], "disable": ["disable", "--now"]}


def user_unit(action: str, unit: str) -> str:
    p = subprocess.run(["systemctl", "--user", *USER_ACTIONS[action], unit],
                       capture_output=True, text=True, timeout=30)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"systemctl --user {action} fallito")
    return p.stdout.strip()


def journal(unit: str, scope: str, lines: int = 200) -> str:
    cmd = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    cmd += ["--user-unit", unit] if scope == "user" else ["-u", unit]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return (p.stdout or p.stderr).strip() or "(nessuna riga)"


# -- omen-fx -------------------------------------------------------------------------

def fx(request: dict, timeout: float = 3.0) -> dict:
    return protocol.send(request, paths.OMEN_FX_SOCKET, timeout, name="omen-fxd")


# -- fan control files (world-readable) ------------------------------------------------

def fan_config() -> dict:
    try:
        return json.loads(paths.FAN_CONF.read_text())
    except (OSError, ValueError):
        return {}


def fan_override() -> dict | None:
    try:
        return json.loads(paths.FAN_OVERRIDE.read_text())
    except (OSError, ValueError):
        return None


def cleaner_state() -> dict:
    try:
        return json.loads(paths.FAN_CLEANER_STATE.read_text())
    except (OSError, ValueError):
        return {}


def cleaner_running(state: dict) -> bool:
    if state.get("state") != "running":
        return False
    pid = state.get("pid")
    if pid:
        try:
            with open(f"/proc/{pid}/stat"):
                pass
        except OSError:
            return False
    started, dur = state.get("started") or 0, state.get("duration") or 90
    return time.time() - started < dur + 60


def cleaner_next(cfg: dict, state: dict) -> float | None:
    """Mirror of FanCleaner.next_auto_time (postponements are not predictable)."""
    if not cfg.get("cleaner_auto"):
        return None
    base = state.get("last_run") or state.get("auto_armed")
    if not base:
        return None
    t = max(base + max(1.0, float(cfg.get("cleaner_interval_hours", 168))) * 3600, time.time())
    a, b = int(cfg.get("cleaner_window_start", 8)), int(cfg.get("cleaner_window_end", 22))
    if (a, b) in ((0, 24), (0, 0)):
        return t
    d = dt.datetime.fromtimestamp(t)

    def inside(h):
        return a <= h < b if a < b else (h >= a or h < b)
    if inside(d.hour):
        return t
    start = d.replace(hour=a % 24, minute=0, second=0, microsecond=0)
    if start <= d:
        start += dt.timedelta(days=1)
    return start.timestamp()
