"""`omen-hub --luce stato|pausa|riprendi [schermo|tastiera]`: the adaptive
brightness from the command line and from the tray switch (the Luminosità e
colore applet runs it). `stato` prints one JSON line; the other two leave a
request for the service (adaptive.REQUEST) and wake it with SIGUSR2.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import adaptive as A

WORDS = {"schermo": "screen", "tastiera": "keyboard", "screen": "screen", "keyboard": "keyboard"}


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def state() -> dict:
    st = A.read_status() or {}
    running = bool(st) and st.get("state") != "fermo" and _alive(st.get("pid"))
    cfg = A.load()
    outs = {}
    for out in A.OUTPUTS:
        o = st.get(out) or {}
        p = o.get("paused") if running else None
        outs[out] = {"attiva": bool(running and o.get("active")), "sospesa": bool(p),
                     "fino": p.get("until") if p else None}
    active = [v for v in outs.values() if v["attiva"]]
    return {"servizio": running, "uscite": outs,
            # what the tray switch shows: on while nothing active is paused
            "accesa": bool(active) and not any(v["sospesa"] for v in active),
            "regolabile": bool(active),
            "pausa_ore": cfg["pause_hours"], "t": time.time()}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "stato"
    if cmd == "stato":
        print(json.dumps(state(), ensure_ascii=False))
        return 0
    if cmd not in ("pausa", "riprendi"):
        print("uso: omen-hub --luce stato|pausa|riprendi [schermo|tastiera]", file=sys.stderr)
        return 2
    outs = [WORDS[a] for a in argv[1:] if a in WORDS] or list(A.OUTPUTS)
    A.write_request(cmd, outs)
    done = subprocess.run(["systemctl", "--user", "kill", "-s", "USR2", "--kill-whom=main", A.UNIT],
                          capture_output=True, text=True)
    if done.returncode:
        print((done.stderr or done.stdout).strip() or "servizio non raggiungibile", file=sys.stderr)
        return 1
    return 0
