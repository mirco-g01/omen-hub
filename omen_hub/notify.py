"""Session notifier: `omen-hub --notify`, run by omen-hub-notify.service.

Started at login and again by omen-hub-notify.path whenever the pacman hook
(or omen-hubd after an apply) rewrites /var/lib/omen-hub/gpufix-check.json.
It recomputes the state itself (as the user: nothing here needs root) and
sends one desktop notification per new situation: the same driver, kernel
and verdict are not announced twice. Clicking «Apri OMEN Hub» starts the hub,
whose bar at the top carries the «Applica correzione» button.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import gpufix

NOTIFY_CODES = ("needed", "lost", "ineffective")
SEEN = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omen-hub" / "gpufix-notified"
HUB = "/usr/local/bin/omen-hub"


def should_notify(st: gpufix.Status, seen: str | None) -> bool:
    return st.code in NOTIFY_CODES and st.signature() != seen


def launch_hub():
    # Its own scope: the hub must outlive this oneshot-like unit.
    argv = ["systemd-run", "--user", "--scope", "--quiet", HUB]
    subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    st = gpufix.status()
    try:
        seen = SEEN.read_text().strip()
    except OSError:
        seen = None
    if not should_notify(st, seen):
        return 0
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(st.signature() + "\n")
    try:
        out = subprocess.run(
            ["notify-send", "--app-name=OMEN Hub", "--icon=omen-hub", "--urgency=normal",
             "--expire-time=0", "--action=open=Apri OMEN Hub", st.title, st.detail],
            capture_output=True, text=True).stdout.strip()
    except OSError:
        return 1
    if out == "open":
        launch_hub()
    return 0
