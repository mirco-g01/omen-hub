"""Everything installed on this laptop over the past months, as one registry.

Each component lists its systemd units (system or user scope), the sleep hook
it owns, and how to install/remove it. Installing and removing always go
through the project's own installer when it has one; the hub never
reimplements an installer.

Status is read as a normal user (systemctl show works for both scopes); the
actions on system scope go through omen-hubd, user scope through
`systemctl --user` straight from the GUI.
"""

from __future__ import annotations

import glob
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

HOME = paths.HOME
FAN_REPO = HOME / "omen-fan-control"
RGB_REPO = HOME / "omen-rgb-keyboard"
BRIGHT_REPO = HOME / "omen-max-linux-brightness-fix"


@dataclass(frozen=True)
class Unit:
    name: str
    scope: str = "system"          # "system" | "user"
    oneshot: bool = False          # runs on an event (resume...): enabled is what counts


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    group: str
    description: str
    units: tuple[Unit, ...] = ()
    hook: str | None = None            # file name in /usr/lib/systemd/system-sleep
    install: tuple[str, ...] | None = None    # argv run as root by omen-hubd
    uninstall: tuple[str, ...] | None = None
    cwd: str | None = None
    source: str | None = None          # repo / origin, shown in the GUI
    notes: str = ""
    removable_hook: bool = True        # False for hooks owned by a package


GROUPS = ("Ventole e termica", "Energia e CPU", "Illuminazione e tastiera",
          "Sospensione", "Desktop")

COMPONENTS: tuple[Component, ...] = (
    Component(
        "fan-control", "OMEN Fan Control", "Ventole e termica",
        "Demone curve ventole (sorgente hybrid), libreria curve e pulizia "
        "automatica a rotazione inversa.",
        units=(Unit("omen-fan-control.service"),),
        install=("/usr/bin/python3", str(FAN_REPO / "omen_cli.py"), "service", "install"),
        uninstall=("/usr/bin/python3", str(FAN_REPO / "omen_cli.py"), "service", "remove"),
        cwd=str(FAN_REPO), source="~/omen-fan-control (fork mirco-g01)",
        notes="Rimuovere il servizio riporta le ventole al firmware (EC)."),
    Component(
        "nvidia-powerd", "NVIDIA Dynamic Boost", "Ventole e termica",
        "Sposta potenza tra CPU e GPU sotto carico (nvidia-powerd).",
        units=(Unit("nvidia-powerd.service"),), source="pacchetto nvidia-utils"),
    Component(
        "auto-cpufreq", "auto-cpufreq", "Energia e CPU",
        "Governor, EPP, turbo e platform profile separati per rete e batteria. "
        "I profili dell'hub scrivono le sue sezioni [charger]/[battery].",
        units=(Unit("auto-cpufreq.service"),), source="pacchetto auto-cpufreq"),
    Component(
        "powertop", "powertop auto-tune", "Energia e CPU",
        "Auto-tune al boot; il drop-in esclude la tastiera OMEN (0d62:54bf) "
        "dall'autosospensione USB.",
        units=(Unit("powertop.service"),), source="pacchetto powertop + override"),
    Component(
        "backlight-sync", "Luminosità reale pannello", "Illuminazione e tastiera",
        "Specchia intel_backlight sul registro EC 0xFD400CF5: la luminosità "
        "smette di essere solo gamma (−3,3 W al 30 %).",
        units=(Unit("omen-backlight-sync.service"),),
        install=("/bin/bash", str(BRIGHT_REPO / "install.sh")), cwd=str(BRIGHT_REPO),
        source="~/omen-max-linux-brightness-fix (v0.3.1 event-driven)"),
    Component(
        "omen-fx", "omen-fx (barra luminosa + tastiera RGB)", "Illuminazione e tastiera",
        "Demone effetti per barra e LampArray per-tasto, notifiche sulla barra, "
        "Alt+luminosità per la tastiera, ripristino dopo il risveglio.",
        units=(Unit("omen-fxd.service"),
               Unit("omen-fx-notifyd.service", "user"), Unit("omen-fx-brightnessd.service", "user")),
        install=("/bin/bash", str(RGB_REPO / "contrib/omen-fx/install.sh"), "install"),
        uninstall=("/bin/bash", str(RGB_REPO / "contrib/omen-fx/install.sh"), "uninstall"),
        cwd=str(RGB_REPO / "contrib/omen-fx"),
        source="~/omen-rgb-keyboard/contrib/omen-fx"),
    Component(
        "omen-f1", "omen-f1 (Formula 1 sulle luci)", "Illuminazione e tastiera",
        "Bandiere e safety car in diretta sulla barra luminosa.",
        units=(Unit("omen-f1.service"),),
        install=("/bin/bash", str(RGB_REPO / "contrib/omen-f1/install.sh"), "install"),
        uninstall=("/bin/bash", str(RGB_REPO / "contrib/omen-f1/install.sh"), "remove"),
        cwd=str(RGB_REPO / "contrib/omen-f1"), source="~/omen-rgb-keyboard/contrib/omen-f1"),
    Component(
        "mute-monitor", "LED muto", "Illuminazione e tastiera",
        "Accende il LED del tasto muto seguendo PipeWire (event-driven, pactl subscribe).",
        units=(Unit("omen-mute-monitor.service", "user"),), source="~/omen-power-fix"),
    Component(
        "kbd-state", "Diagnostica riga Fn", "Illuminazione e tastiera",
        "Logga a ogni risveglio lo stato dell'MCU tastiera (byte 0x0C). "
        "Solo diagnostica: il guasto è risolto da sleep_no_lps0.",
        units=(Unit("omen-kbd-state.service", oneshot=True),), source="/usr/local/bin/omen-kbd-state"),
    Component(
        "hook-keyboard", "Hook tastiera (sleep)", "Sospensione",
        "pre: tastiera nera durante il sonno (senza LPS0 l'EC non la spegne); "
        "post: ripristino i8042 e tastiera esterna.",
        hook="keyboard-fix.sh"),
    Component(
        "hook-ec-standby", "Flag standby EC (PECF)", "Sospensione",
        "Scrive PECF=1 nell'EC prima del sonno: senza, l'EC fa girare le ventole "
        "dopo 2–5 minuti di s2idle.",
        hook="ec-standby-flag.sh", notes="Richiede ec_sys write_support=1."),
    Component(
        "hook-vram", "Libera VRAM prima del sonno", "Sospensione",
        "Ferma llama-server/ollama prima della sospensione (10 GiB di VRAM = 25 s di nero).",
        hook="free-vram-on-suspend.sh"),
    Component(
        "hook-nvidia", "Hook NVIDIA", "Sospensione",
        "Hook di nvidia-utils per suspend/resume del driver.",
        hook="nvidia", removable_hook=False, source="pacchetto nvidia-utils",
        units=(Unit("nvidia-suspend.service", oneshot=True), Unit("nvidia-resume.service", oneshot=True))),
    Component(
        "fx-resume", "omen-fx dopo il risveglio", "Sospensione",
        "Riavvia omen-fx-notifyd dopo il thaw (mai dagli hook: user.slice è congelata).",
        units=(Unit("omen-fx-notifyd-resume.service", oneshot=True),)),
    Component(
        "baloo", "Baloo (indicizzazione)", "Desktop",
        "Solo nomi file, confinato agli E-core con CPUQuota 25 % "
        "(teneva un P-core a 100 °C per ore).",
        units=(Unit("kde-baloo.service", "user"),)),
    Component(
        "ds4-desk", "Controller PS4 desktop", "Desktop",
        "DualShock 4 come tastiera/mouse, layer finestre e dettatura Parakeet.",
        units=(Unit("ds4-desk.service", "user"),), source="~/.local/share/ds4-desk"),
    Component(
        "dpms-guard", "Guardiano schermo spento", "Desktop",
        "Mette in pausa plasmashell quando tutti gli schermi sono spenti e lui gira a vuoto "
        "(bug kwin/plasmashell dopo lo standby del monitor HDMI: CPU a 100 °C, ventole a 6000 RPM). "
        "Lo riprende appena uno schermo si riaccende.",
        units=(Unit("plasmashell-dpms-guard.service", "user"),),
        source="~/.local/bin/plasmashell-dpms-guard.py (12/10/2026)"),
    Component(
        "deskterm", "Terminali sul desktop", "Desktop",
        "Mantiene i pannelli terminale agganciati ai monitor collegati.",
        units=(Unit("deskterm-panels.service", "user"),)),
)

BY_ID = {c.id: c for c in COMPONENTS}


def system_units() -> set[str]:
    return {u.name for c in COMPONENTS for u in c.units if u.scope == "system"}


# -- status ------------------------------------------------------------------

PROPS = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState")


def _show(units: list[str], user: bool) -> dict[str, dict]:
    if not units:
        return {}
    cmd = ["systemctl"] + (["--user"] if user else []) + \
        ["show", "--no-pager", "-p", ",".join(PROPS)] + units
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    result, cur = {}, {}
    for block in out.split("\n\n"):
        cur = dict(line.split("=", 1) for line in block.strip().splitlines() if "=" in line)
        if cur.get("Id"):
            result[cur["Id"]] = cur
    return result


def hook_state(name: str) -> str:
    """'installed' | 'disabled' (taken out by the hub) | 'inactive' (present
    but not executable) | 'missing'."""
    p = paths.SLEEP_DIR / name
    if p.exists():
        return "installed" if os.access(p, os.X_OK) else "inactive"
    if (paths.DISABLED_HOOKS / name).exists():
        return "disabled"
    return "missing"


def statuses() -> dict[str, dict]:
    sys_units = sorted({u.name for c in COMPONENTS for u in c.units if u.scope == "system"})
    usr_units = sorted({u.name for c in COMPONENTS for u in c.units if u.scope == "user"})
    shown = {("system", k): v for k, v in _show(sys_units, False).items()}
    shown.update({("user", k): v for k, v in _show(usr_units, True).items()})
    out = {}
    for c in COMPONENTS:
        units = []
        for u in c.units:
            info = shown.get((u.scope, u.name), {})
            units.append({
                "name": u.name, "scope": u.scope,
                "load": info.get("LoadState", "unknown"),
                "active": info.get("ActiveState", "unknown"),
                "sub": info.get("SubState", ""),
                "enabled": info.get("UnitFileState", ""),
            })
        out[c.id] = {"units": units, "hook": hook_state(c.hook) if c.hook else None,
                     "summary": summarize(c, units)}
    return out


def summarize(c: Component, units: list[dict]) -> str:
    """One word for the row: ok | warn | off | missing | failed."""
    if c.hook:
        hs = hook_state(c.hook)
        if hs != "installed":
            return "off" if hs in ("disabled", "inactive") else "missing"
    if not units:
        return "ok"
    if any(u["active"] == "failed" for u in units):
        return "failed"
    by_name = {u.name: u for u in c.units}
    loaded = [u for u in units if u["load"] == "loaded"]
    if not loaded:
        return "missing"

    def good(u):
        if by_name[u["name"]].oneshot:
            return u["enabled"] in ("enabled", "static")
        return u["active"] in ("active", "activating", "reloading")
    enabled = [u for u in loaded if u["enabled"] in ("enabled", "enabled-runtime", "static", "alias")]
    if all(good(u) for u in loaded) and len(loaded) == len(units):
        return "ok"
    if not any(good(u) for u in loaded) and not enabled:
        return "off"
    return "warn"


# -- health checks -------------------------------------------------------------

@dataclass
class Check:
    title: str
    ok: bool | None          # None = informative
    detail: str
    hint: str = ""


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _grep_dir(pattern: str, globpat: str) -> list[str]:
    hits = []
    rx = re.compile(pattern)
    for f in glob.glob(globpat):
        try:
            for line in Path(f).read_text().splitlines():
                if not line.lstrip().startswith("#") and rx.search(line):
                    hits.append(f"{Path(f).name}: {line.strip()}")
        except OSError:
            pass
    return hits


def health_checks() -> list[Check]:
    checks: list[Check] = []
    cmd = (_read("/proc/cmdline") or "").split()

    ok = "acpi_x86.sleep_no_lps0=1" in cmd
    checks.append(Check(
        "Tasti F2–F4 dopo la sospensione", ok,
        "acpi_x86.sleep_no_lps0=1 sulla riga di boot" if ok else "parametro assente dalla riga di boot",
        "" if ok else "Senza, il _DSM LPS0 passa F2/F3/F4 al software host e la tastiera "
                      "resta rotta fino allo spegnimento."))
    ok = "i8042.reset=1" in cmd
    checks.append(Check("Reset controller PS/2 al resume", ok,
                        "i8042.reset=1 presente" if ok else "i8042.reset=1 assente"))

    ws = _read("/sys/module/ec_sys/parameters/write_support")
    checks.append(Check(
        "Scrittura EC per il flag standby", ws == "Y",
        f"ec_sys write_support={ws or 'modulo non caricato'}",
        "" if ws == "Y" else "ec-standby-flag.sh non può scrivere PECF: ventole in sleep."))

    hits = _grep_dir(r"NVreg_DynamicPowerManagement\s*=\s*0x0?2", "/etc/modprobe.d/*.conf")
    checks.append(Check(
        "RTD3 GPU (D3cold a riposo)", bool(hits),
        hits[0] if hits else "NVreg_DynamicPowerManagement=0x02 non trovato in /etc/modprobe.d",
        "" if hits else "Senza RTD3 la RTX resta accesa: ~+6,5 W a riposo."))

    sleep_dir = paths.SLEEP_DIR
    stray = []
    known = {c.hook for c in COMPONENTS if c.hook}
    for f in sorted(sleep_dir.glob("*")) if sleep_dir.is_dir() else []:
        try:
            mode = f.stat().st_mode
        except OSError:
            continue
        if f.name not in known and stat.S_ISREG(mode) and mode & 0o111:
            stray.append(f.name)
    checks.append(Check(
        "Hook di sospensione estranei", not stray,
        ", ".join(stray) if stray else "solo gli hook noti sono eseguibili",
        "systemd-sleep esegue in parallelo ogni file eseguibile, .bak compresi."
        if stray else ""))
    inactive = [f.name for f in sleep_dir.glob("*") if f.name not in known] if sleep_dir.is_dir() else []
    if inactive and not stray:
        checks.append(Check("File inerti in system-sleep", None, ", ".join(inactive),
                            "Non eseguibili, quindi ignorati da systemd."))

    try:
        dkms = subprocess.run(["dkms", "status"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        dkms = ""
    kernel = os.uname().release
    for mod, why in (("hp-wmi-omen", "pwm ventole, platform_profile, cTGP"),
                     ("omen-rgb-keyboard", "barra luminosa e LED tastiera"),
                     ("acpi_call", "pulizia ventole (rotazione inversa)"),
                     ("omen_wmi_boost", "boost GPU")):
        line = next((l for l in dkms.splitlines() if l.startswith(mod + "/")), "")
        ok = kernel in line and "installed" in line
        checks.append(Check(f"DKMS {mod}", ok if line else False,
                            line or "non registrato in DKMS", "" if ok else f"Serve per: {why}."))

    rtd = Path("/sys/bus/pci/devices/0000:02:00.0/power/runtime_status")
    st = _read(rtd)
    if st:
        hdmi = any(_read(f"{c}/status") == "connected" for c in glob.glob("/sys/class/drm/card*-HDMI-*"))
        if hdmi:
            checks.append(Check("Stato GPU", None, f"runtime_status = {st} (HDMI collegato)",
                                "Con il monitor HDMI la GPU resta accesa: la porta è sulla NVIDIA."))
        else:
            ok = st != "active"
            checks.append(Check("GPU spenta a riposo (D3cold)", ok, f"runtime_status = {st}, HDMI non collegato",
                                "" if ok else "Accesa senza monitor: ~6–10 W sprecati. La pagina Test → GPU "
                                              "mostra chi la tiene accesa."))

    # 12/10/2026: what kept the dGPU awake forever
    acf = _read(paths.AUTOCPUFREQ_CONF) or ""
    sections = {}
    cur = None
    for line in acf.splitlines():
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"\s*enforce_platform_profile\s*=\s*(\S+)", line)
        if m and cur:
            sections[cur] = m.group(1).lower()
    ok = all(sections.get(k) == "false" for k in ("charger", "battery"))
    checks.append(Check(
        "auto-cpufreq non riscrive il profilo", ok,
        "enforce_platform_profile = false in [charger] e [battery]" if ok
        else f"enforce_platform_profile: {sections or 'assente (default true)'}",
        "" if ok else "Riscrivendo platform_profile ogni pochi secondi, hp-wmi manda al firmware i comandi "
                      "GPU (WMI 0x22) e la NVIDIA non si spegne mai."))
    src = _read("/usr/src/hp-wmi-omen-1.0/hp-wmi.c") or ""
    ok = "already active, skipping" in src
    checks.append(Check(
        "hp-wmi ignora i profili ripetuti", ok,
        "patch presente nel sorgente DKMS" if ok else "patch assente in /usr/src/hp-wmi-omen-1.0",
        "" if ok else "Senza, ogni scrittura dello stesso platform_profile sveglia la GPU."))
    hits = [h for h in _grep_dir(r"power/control.*\"on\"", "/etc/udev/rules.d/*.rules")
            if "02:00.0" in h or "10de" in h]
    checks.append(Check(
        "Nessuna regola udev che tiene accesa la GPU", not hits,
        hits[0] if hits else "nessuna regola power/control=on sulla NVIDIA",
        "Vieta l'RTD3 a ogni avvio (finché powertop non la riporta su auto)." if hits else ""))
    env = " ".join(_read(f) or "" for f in glob.glob(str(HOME / ".config/environment.d/*.conf")))
    ok = "MOZ_DRM_DEVICE" in env and "00:02.0" in env
    checks.append(Check(
        "Firefox decodifica i video sull'Intel", ok,
        "MOZ_DRM_DEVICE punta alla iGPU" if ok else "MOZ_DRM_DEVICE non impostato",
        "" if ok else "Firefox sceglie il primo render node (la NVIDIA) e la tiene accesa durante i video."))

    zram = _read("/sys/block/zram0/disksize")
    swp = _read("/proc/sys/vm/swappiness")
    if zram:
        checks.append(Check("zram + swappiness", None,
                            f"zram0 {int(zram) / 2**30:.0f} GiB, vm.swappiness={swp}"))

    over = HOME / ".config/systemd/user/kde-baloo.service.d/override.conf"
    txt = _read(over) or ""
    ok = "taskset" in txt and "CPUQuota" in txt
    checks.append(Check("Baloo confinato agli E-core", ok,
                        "override con taskset + CPUQuota" if ok else "override assente o incompleto"))

    pd = HOME / ".config/systemd/user/plasma-powerdevil.service.d/override.conf"
    ok = "POWERDEVIL_NO_DDCUTIL=1" in (_read(pd) or "")
    checks.append(Check("powerdevil senza ddcutil", ok,
                        "POWERDEVIL_NO_DDCUTIL=1" if ok else "drop-in assente",
                        "" if ok else "ddcutil faceva crashare powerdevil all'hotplug HDMI."))

    lock = HOME / ".config/systemd/user/keyboxd.service.d/stale-locks.conf"
    checks.append(Check("Pulizia lock GnuPG (kwallet)", lock.exists(),
                        "ExecStartPre gnupg-stale-locks" if lock.exists() else "drop-in assente"))
    return checks
