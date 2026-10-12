"""The omen-fx effect engine, borrowed rather than copied.

The light studio previews effects by running the *same* generators omen-fxd
runs, so what it draws is what the keyboard and bar will do. Those modules
live in the omen-fx install; a copy here would drift the first time an effect
changed there. The installed, root-owned copy wins over the source tree,
because that is the one the running daemon was started from.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

CANDIDATES = (
    os.environ.get("OMEN_FX_LIB", ""),
    "/usr/local/lib/omen-fx",
    str(Path.home() / "omen-rgb-keyboard" / "contrib" / "omen-fx"),
)

fx = lay = surface = led = themes = None
LOCATION: str | None = None
ERROR: str | None = None


def load() -> bool:
    """Import the engine once; False (with ERROR set) when it is missing."""
    global fx, lay, surface, led, themes, LOCATION, ERROR
    if fx is not None:
        return True
    for base in CANDIDATES:
        if not base or not (Path(base) / "omen_fx" / "effects.py").is_file():
            continue
        if base not in sys.path:
            sys.path.insert(0, base)
        try:
            fx = importlib.import_module("omen_fx.effects")
            lay = importlib.import_module("omen_fx.layouts")
            surface = importlib.import_module("omen_fx.surface")
            led = importlib.import_module("omen_fx.led")
            themes = importlib.import_module("omen_fx.themes")
        except Exception as exc:          # a broken install must not take the hub down
            fx = None
            ERROR = f"{base}: {exc}"
            continue
        LOCATION = base
        ERROR = None
        return True
    ERROR = ERROR or "omen-fx non trovato (né in /usr/local/lib/omen-fx né nel sorgente)"
    return False
