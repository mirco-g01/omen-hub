"""The adaptive brightness switch in the tray's «Luminosità e colore».

Plasma 6.8 compiles that applet into powerdevil's plugin, so it cannot be
patched in place. OMEN Hub ships a twin instead (plasma/org.mircogiorgi.brightness:
the upstream QML of the installed powerdevil version, the original applet's
translations, plus AdaptiveItem.qml) and swaps it for the original in every
system tray. Everything is per user and reversible: `rimuovi` puts the
original back. The swap needs plasmashell stopped (it rewrites its config on
exit), so both directions restart it.

    omen-hub --vassoio stato|installa|rimuovi|aggiorna
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ORIG = "org.kde.plasma.brightness"
OURS = "org.mircogiorgi.brightness"
SRC = Path(__file__).resolve().parent.parent / "plasma" / OURS
DEST = Path.home() / ".local/share/plasma/plasmoids" / OURS
APPLETSRC = Path.home() / ".config/plasma-org.kde.plasma.desktop-appletsrc"
SHELL_UNIT = "plasma-plasmashell.service"
LIST_KEYS = ("extraItems", "knownItems", "shownItems", "hiddenItems")


def _base(meta: Path) -> str | None:
    try:
        return json.loads(meta.read_text()).get("X-OmenHub-Base")
    except (OSError, ValueError):
        return None


def powerdevil_version() -> str | None:
    try:
        out = subprocess.run(["pacman", "-Q", "powerdevil"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"powerdevil (\d+(?:\.\d+)*)", out)
    return m.group(1) if m else None


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """The file as (header, lines) blocks; the first block may have no header."""
    out, head, lines = [], "", []
    for line in text.splitlines(keepends=True):
        if line.startswith("[") and line.rstrip("\n").endswith("]"):
            out.append((head, lines))
            head, lines = line.rstrip("\n"), [line]
        else:
            lines.append(line)
    out.append((head, lines))
    return out


def _applets(text: str, plugin: str) -> set[str]:
    """Headers of the applets running `plugin`. Tray items are nested one
    level down: "[Containments][214][Applets][224][Applets][251]"."""
    return {h for h, lines in _sections(text)
            if re.fullmatch(r"\[Containments\]\[\d+\](\[Applets\]\[\d+\])+", h)
            and f"plugin={plugin}\n" in lines}


def retarget(text: str, old: str, new: str) -> tuple[str, int]:
    """Make the trays run `new` where they ran `old`.

    The applet groups of `old` are switched to `new`, or dropped if `new` is
    already there (plasmashell re-adds an applet it no longer "knows" as a
    duplicate). In the lists, `new` takes the place of `old` among the shown
    ones; `knownItems` keeps both, so that neither comes back on its own."""
    have_new = bool(_applets(text, new))
    gone = _applets(text, old) if have_new else set()
    n = 0
    out = []
    for head, lines in _sections(text):
        if any(head == g or head.startswith(g + "[") for g in gone):
            n += 1
            continue                          # a duplicate of the original, with its subgroups
        for line in lines:
            body = line.rstrip("\n")
            key, sep, val = body.partition("=")
            if body == f"plugin={old}":
                line = f"plugin={new}\n"
                n += 1
            elif sep and key in LIST_KEYS:
                items = val.split(",") if val else []
                if key == "knownItems":
                    if old in items and new not in items:
                        items.append(new)
                        n += 1
                elif old in items:
                    items = [new if v == old else v for v in items]
                    items = [v for i, v in enumerate(items) if v not in items[:i]]
                    n += 1
                line = f"{key}={','.join(items)}\n"
            out.append(line)
    return "".join(out), n


def status() -> dict:
    try:
        cfg = APPLETSRC.read_text()
    except OSError:
        cfg = ""
    base = _base(SRC / "metadata.json")
    pd = powerdevil_version()
    return {
        "pacchetto": DEST.exists(),
        "nel_vassoio": f"plugin={OURS}" in cfg,
        "originale_nel_vassoio": f"plugin={ORIG}" in cfg,
        "base": base, "powerdevil": pd,
        # the twin is built on one powerdevil version: another one may need a new copy
        "allineato": bool(base and pd and base.split()[-1] == pd),
    }


def _copy():
    if not SRC.exists():
        raise RuntimeError(f"pacchetto mancante: {SRC}")
    tmp = DEST.with_name(DEST.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(SRC, tmp)
    shutil.rmtree(DEST, ignore_errors=True)
    tmp.rename(DEST)


def _with_shell_stopped(old: str, new: str) -> int:
    subprocess.run(["systemctl", "--user", "stop", SHELL_UNIT], check=True, timeout=60)
    try:
        text = APPLETSRC.read_text()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(APPLETSRC, APPLETSRC.with_name(APPLETSRC.name + f".bak-omen-hub-{stamp}"))
        new_text, n = retarget(text, old, new)
        if n:
            tmp = APPLETSRC.with_name(APPLETSRC.name + ".omen-hub.tmp")
            tmp.write_text(new_text)
            tmp.replace(APPLETSRC)
        return n
    finally:
        subprocess.run(["systemctl", "--user", "start", SHELL_UNIT], timeout=60)


def install() -> str:
    _copy()
    n = _with_shell_stopped(ORIG, OURS)
    return f"interruttore aggiunto: {n} voci del vassoio aggiornate, plasmashell riavviato"


def remove() -> str:
    n = _with_shell_stopped(OURS, ORIG)
    shutil.rmtree(DEST, ignore_errors=True)
    return f"pannello originale rimesso: {n} voci del vassoio aggiornate, plasmashell riavviato"


def refresh() -> str:
    """After an OMEN Hub update: new QML for an installed twin (it is read at
    the next plasmashell start)."""
    if not DEST.exists():
        return "interruttore non installato: niente da aggiornare"
    _copy()
    return "pacchetto aggiornato (vale dal prossimo avvio di plasmashell)"


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "stato"
    try:
        if cmd == "stato":
            print(json.dumps(status(), ensure_ascii=False))
        elif cmd == "installa":
            print(install())
        elif cmd == "rimuovi":
            print(remove())
        elif cmd == "aggiorna":
            print(refresh())
        else:
            print("uso: omen-hub --vassoio stato|installa|rimuovi|aggiorna", file=sys.stderr)
            return 2
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"errore: {exc}", file=sys.stderr)
        return 1
    return 0
