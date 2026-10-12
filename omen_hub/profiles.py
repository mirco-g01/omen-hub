"""Power profiles, OMEN Gaming Hub style: a named set of CPU/platform/fan
settings, with one profile assigned to mains and one to battery.

The CPU half is applied through auto-cpufreq (it already switches between its
[charger] and [battery] sections on plug/unplug); the fan half is applied by
omen-hubd when the power source changes.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import cpuconf

NAME_RE = re.compile(r"^[\w][\w .()+-]{0,39}$", re.UNICODE)
FAN_MODES = ("curve", "auto", "max")

# Built-in starting points. The first run also adds the two profiles found in
# auto-cpufreq.conf ("... (attuale)") and assigns them, so nothing changes
# until the user picks something else.
PRESETS: dict[str, dict] = {
    "Silenzioso": {
        "description": "Batteria al massimo: niente turbo, piattaforma low-power.",
        "governor": "powersave", "epp": "power", "turbo": "never",
        "platform_profile": "low-power", "min_freq": None, "max_freq": None,
        "fan": None, "color": "#5b8def",
    },
    "Bilanciato": {
        "description": "Uso quotidiano: turbo su richiesta, piattaforma balanced.",
        "governor": "powersave", "epp": "balance_power", "turbo": "auto",
        "platform_profile": "balanced", "min_freq": None, "max_freq": None,
        "fan": None, "color": "#009782",
    },
    "Prestazioni": {
        "description": "Turbo sempre, cTGP GPU attivo (platform performance).",
        "governor": "powersave", "epp": "balance_performance", "turbo": "always",
        "platform_profile": "performance", "min_freq": 1400, "max_freq": None,
        "fan": None, "color": "#e8833a",
    },
    "Massime": {
        "description": "EPP performance, turbo sempre, cTGP GPU e ventole al massimo: "
                       "per benchmark e giochi.",
        "governor": "powersave", "epp": "performance", "turbo": "always",
        "platform_profile": "performance", "min_freq": None, "max_freq": None,
        "fan": {"mode": "max", "curve": None}, "color": "#d9534f",
    },
}


def validate_profile(p: dict, platform_choices: list[str] | None = None,
                     curves: list[str] | None = None,
                     freq_range: tuple[int, int] | None = None) -> dict:
    if not isinstance(p, dict):
        raise ValueError("profilo non valido")
    out = {
        "description": str(p.get("description", ""))[:200],
        "governor": p.get("governor", "powersave"),
        "epp": p.get("epp", "default"),
        "turbo": p.get("turbo", "auto"),
        "platform_profile": p.get("platform_profile") or None,
        "min_freq": p.get("min_freq"),
        "max_freq": p.get("max_freq"),
        "fan": p.get("fan"),
        "color": p.get("color") or "#888888",
        "pl1": p.get("pl1"), "pl2": p.get("pl2"), "gpu_clock": p.get("gpu_clock"),
        "ratio_p": p.get("ratio_p"), "ratio_e": p.get("ratio_e"),
    }
    if out["governor"] not in cpuconf.GOVERNORS:
        raise ValueError(f"governor non valido: {out['governor']}")
    if out["epp"] not in cpuconf.EPPS:
        raise ValueError(f"EPP non valido: {out['epp']}")
    if out["turbo"] not in cpuconf.TURBO:
        raise ValueError(f"turbo non valido: {out['turbo']}")
    if out["platform_profile"] is not None and platform_choices and \
            out["platform_profile"] not in platform_choices:
        raise ValueError(f"platform_profile non disponibile: {out['platform_profile']}")
    for k in ("min_freq", "max_freq"):
        v = out[k]
        if v in (None, "", 0):
            out[k] = None
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} non numerico") from None
        if freq_range and not (freq_range[0] <= v <= freq_range[1]):
            raise ValueError(f"{k}={v} MHz fuori dall'intervallo {freq_range[0]}–{freq_range[1]}")
        out[k] = v
    if out["min_freq"] and out["max_freq"] and out["min_freq"] > out["max_freq"]:
        raise ValueError("frequenza minima maggiore della massima")
    fan = out["fan"]
    if fan is not None:
        if not isinstance(fan, dict) or fan.get("mode") not in FAN_MODES:
            raise ValueError("impostazione ventole non valida")
        curve = fan.get("curve") or None
        if fan["mode"] == "curve" and curves is not None and curve and curve not in curves:
            raise ValueError(f"curva ventole inesistente: {curve}")
        out["fan"] = {"mode": fan["mode"], "curve": curve if fan["mode"] == "curve" else None}
    for k, (lo, hi), what in (("pl1", (10, 250), "PL1"), ("pl2", (10, 250), "PL2"),
                              ("gpu_clock", (300, 3500), "clock massimo GPU")):
        v = out[k]
        if v in (None, "", 0):
            out[k] = None
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{what} non numerico") from None
        if not lo <= v <= hi:
            raise ValueError(f"{what} {v} fuori da {lo}–{hi}")
        out[k] = v
    if out["pl1"] and out["pl2"] and out["pl2"] < out["pl1"]:
        raise ValueError("PL2 non può essere minore di PL1")
    for field, label in (("ratio_p", "P-core"), ("ratio_e", "E-core")):
        rp = out[field]
        if rp in (None, "", 0, []):
            out[field] = None
            continue
        if isinstance(rp, int):
            rp = [rp] * 8
        if not isinstance(rp, list) or len(rp) != 8:
            raise ValueError(f"moltiplicatori {label}: servono 8 valori (uno per gruppo di core attivi)")
        clean = []
        for x in rp:
            if x in (None, 0, ""):
                clean.append(None)
                continue
            try:
                x = int(x)
            except (TypeError, ValueError):
                raise ValueError(f"moltiplicatore {label} non numerico") from None
            if not 8 <= x <= 60:
                raise ValueError(f"moltiplicatore {label} ×{x} fuori da ×8–×60")
            clean.append(x)
        out[field] = None if all(c is None for c in clean) else clean
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(out["color"])):
        out["color"] = "#888888"
    return out


def validate_store(store: dict, **kw) -> dict:
    profiles = store.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("serve almeno un profilo")
    clean = {}
    for name, p in profiles.items():
        if not NAME_RE.fullmatch(str(name)):
            raise ValueError(f"nome profilo non valido: {name!r}")
        clean[str(name)] = validate_profile(p, **kw)
    assign = store.get("assign") or {}
    out_assign = {}
    for src in ("ac", "battery"):
        name = assign.get(src)
        if name not in clean:
            raise ValueError(f"profilo assegnato a '{src}' inesistente: {name!r}")
        out_assign[src] = name
    return {"version": 1, "profiles": clean, "assign": out_assign}


def seed_from_cpufreq(conf_text: str | None) -> dict:
    profiles = copy.deepcopy(PRESETS)
    assign = {"ac": "Prestazioni", "battery": "Bilanciato"}
    if conf_text:
        try:
            sections = cpuconf.parse(conf_text)
        except Exception:
            sections = {}
        for src, label, color in (("ac", "Rete (attuale)", "#c08a2e"),
                                  ("battery", "Batteria (attuale)", "#3f9d6a")):
            sec = sections.get(cpuconf.SECTIONS[src])
            if sec is None:
                continue
            p = cpuconf.section_to_profile(sec)
            p.update(description="Importato da /etc/auto-cpufreq.conf al primo avvio.",
                     fan=None, color=color)
            profiles[label] = p
            assign[src] = label
    return {"version": 1, "profiles": profiles, "assign": assign}


def load(path: Path, cpufreq_conf: Path | None = None) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        text = None
        if cpufreq_conf is not None:
            try:
                text = cpufreq_conf.read_text()
            except OSError:
                pass
        return seed_from_cpufreq(text)
