"""Every path the hub touches, in one place.

OMEN_HUB_ROOT prefixes the files the daemon *writes* (config, hooks, state), so
the whole daemon can be exercised as a normal user against a scratch tree.
sysfs is always read from the real system.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = os.environ.get("OMEN_HUB_ROOT", "")
DRY_RUN = bool(os.environ.get("OMEN_HUB_DRY_RUN"))


def P(path: str) -> Path:
    return Path(ROOT + path) if ROOT else Path(path)


SOCKET = os.environ.get("OMEN_HUB_SOCKET", "/run/omen-hub/hub.sock")

ETC = P("/etc/omen-hub")
PROFILES = ETC / "profiles.json"
HUBD_CONF = ETC / "hubd.json"

STATE_DIR = P("/var/lib/omen-hub")
BACKUP_DIR = STATE_DIR / "backup"
# Hooks taken out of service. Never inside system-sleep/: systemd-sleep runs
# every executable there, backups included.
DISABLED_HOOKS = STATE_DIR / "disabled-hooks"
# Fan mode the user had before a profile imposed one; restored when the
# active profile no longer says anything about fans.
FAN_OVERRIDE = STATE_DIR / "fan-override.json"

AUTOCPUFREQ_CONF = P("/etc/auto-cpufreq.conf")
FAN_CONF_DIR = P("/etc/omen-fan-control")
FAN_CONF = FAN_CONF_DIR / "config.json"
FAN_CLEANER_STATE = FAN_CONF_DIR / "cleaner_state.json"
FAN_CLEANER_REQUEST = FAN_CONF_DIR / "cleaner.request"
FAN_CLEANER_STOP = FAN_CONF_DIR / "cleaner.stop"

SLEEP_DIR = P("/usr/lib/systemd/system-sleep")

# Hook sources shipped with the hub (snapshotted from the live system by
# install.sh), used to reinstall a hook that was removed.
PAYLOAD = Path(__file__).resolve().parent.parent / "payload"

OMEN_FX_SOCKET = "/run/omen-fx/control.sock"

HOME = Path(os.environ.get("OMEN_HUB_USER_HOME", "/home/mircogiorgi"))
POWERDEVILRC = Path.home() / ".config" / "powerdevilrc"
