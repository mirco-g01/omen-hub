"""auto-cpufreq configuration: read it, and edit only the keys the hub manages.

The file is hand-written and full of comments explaining past decisions (why
turbo is "always" on mains, why the battery section sets platform_profile...),
so it is edited line by line: managed keys are replaced in place, removed or
appended at the end of their section; everything else is kept verbatim.
"""

from __future__ import annotations

import configparser
import re

SECTIONS = {"ac": "charger", "battery": "battery"}

GOVERNORS = ("powersave", "performance")
EPPS = ("default", "performance", "balance_performance", "balance_power", "power")
TURBO = ("always", "auto", "never")

# profile field -> auto-cpufreq key
KEYMAP = {
    "governor": "governor",
    "epp": "energy_performance_preference",
    "turbo": "turbo",
    "platform_profile": "platform_profile",
    "min_freq": "scaling_min_freq",
    "max_freq": "scaling_max_freq",
}


def parse(text: str) -> dict[str, dict[str, str]]:
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"), strict=False,
                                   interpolation=None)
    cp.read_string(text)
    return {s: dict(cp.items(s)) for s in cp.sections()}


def section_to_profile(values: dict[str, str]) -> dict:
    """auto-cpufreq section -> the CPU part of a hub profile (MHz for freqs)."""
    def mhz(key):
        try:
            return int(values[key]) // 1000
        except (KeyError, ValueError):
            return None
    return {
        "governor": values.get("governor", "powersave"),
        "epp": values.get("energy_performance_preference", "default"),
        "turbo": values.get("turbo", "auto"),
        "platform_profile": values.get("platform_profile"),
        "min_freq": mhz("scaling_min_freq"),
        "max_freq": mhz("scaling_max_freq"),
    }


def profile_to_values(profile: dict) -> dict[str, str | None]:
    """Hub profile -> {auto-cpufreq key: value or None (= remove the key)}."""
    out: dict[str, str | None] = {}
    for field, key in KEYMAP.items():
        v = profile.get(field)
        if v in (None, ""):
            out[key] = None
        elif field == "epp" and profile.get("governor") == "performance":
            # intel_pstate refuses EPP writes under the performance governor
            # (EBUSY); auto-cpufreq would retry every cycle and flood the journal
            out[key] = None
        elif field in ("min_freq", "max_freq"):
            out[key] = str(int(v) * 1000)
        else:
            out[key] = str(v)
    return out


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s[0] in "#;[":
        return None
    m = re.match(r"([A-Za-z0-9_.-]+)\s*[=:]", s)
    return m.group(1).lower() if m else None


def update_section(text: str, section: str, values: dict[str, str | None]) -> str:
    lines = text.splitlines()
    start = end = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1).strip() == section:
                start = i
    if start is None:
        new = [] if not lines or not lines[-1].strip() else [""]
        new.append(f"[{section}]")
        new += [f"{k} = {v}" for k, v in values.items() if v is not None]
        return "\n".join(lines + new) + "\n"
    if end is None:
        end = len(lines)

    body = lines[start + 1:end]
    pending = dict(values)
    out_body = []
    for line in body:
        key = _key_of(line)
        if key in pending:
            v = pending.pop(key)
            if v is not None:
                out_body.append(f"{key} = {v}")
            continue
        out_body.append(line)
    # append what was not already there, before the section's trailing blanks
    tail = 0
    while tail < len(out_body) and not out_body[len(out_body) - 1 - tail].strip():
        tail += 1
    insert_at = len(out_body) - tail
    added = [f"{k} = {v}" for k, v in pending.items() if v is not None]
    out_body[insert_at:insert_at] = added
    result = lines[:start + 1] + out_body + lines[end:]
    return "\n".join(result) + "\n"


def render(text: str, ac_profile: dict, battery_profile: dict) -> str:
    text = update_section(text, SECTIONS["ac"], profile_to_values(ac_profile))
    text = update_section(text, SECTIONS["battery"], profile_to_values(battery_profile))
    return text
