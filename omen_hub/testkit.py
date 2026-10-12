"""Measurements and the report behind the Test page (no Qt in here).

What the tests read, and why it is read this way, comes from the sessions of
September/October 2026 on this machine:

- battery current_now x voltage_now is the only real total-power figure, and
  only on battery; RAPL (package power, root-only) covers the CPU on mains too;
- the NVIDIA state is read from power/runtime_status only: nvidia-smi (and
  lspci) wake it from D3cold, so they are used only when it is already awake;
- the fans during a suspend are seen in the first samples after the thaw
  (they take seconds to change speed), with a 0.25 s sampler that keeps
  running across the sleep;
- anything animated on screen costs power, so the idle measurements are
  taken with the page static.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, protocol, sysinfo

REPORT_DIR = Path.home() / "omen-hub-reports"
GPU_BURN = paths.HOME / "gpu-burn" / "gpu_burn"


def _read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _int(path, default=None):
    v = _read(path)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def run(argv: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# -- live probes -------------------------------------------------------------------

class Probes:
    """Cheap sysfs reads for the tests; one instance per run."""

    def __init__(self):
        self.sampler = sysinfo.Sampler()
        self.gpu = sysinfo.find_nvidia_gpu()
        self._rapl_prev: tuple[float, int] | None = None
        self._rapl_max = 0

    # display ---------------------------------------------------------------
    @staticmethod
    def connectors() -> dict[str, dict]:
        out = {}
        for c in sorted(glob.glob("/sys/class/drm/card*-*")):
            conn = Path(c).name.split("-", 1)[1]
            out[conn] = {"status": _read(f"{c}/status"), "dpms": _read(f"{c}/dpms")}
        return out

    def hdmi_connected(self) -> bool:
        return any(k.startswith("HDMI") and v["status"] == "connected"
                   for k, v in self.connectors().items())

    def screens_off(self) -> bool:
        conns = [v for v in self.connectors().values() if v["status"] == "connected"]
        return bool(conns) and all(v["dpms"] != "On" for v in conns)

    def ac_online(self) -> bool | None:
        v = _int("/sys/class/power_supply/AC/online")
        return None if v is None else v == 1

    # GPU -------------------------------------------------------------------
    def gpu_state(self) -> str | None:
        return _read(self.gpu / "power" / "runtime_status") if self.gpu else None

    def gpu_suspended_ms(self) -> int | None:
        return _int(self.gpu / "power" / "runtime_suspended_time") if self.gpu else None

    @staticmethod
    def gpu_holders() -> list[dict] | None:
        try:
            return protocol.send({"cmd": "test.gpu_holders"}, timeout=10)["holders"]
        except (protocol.HubError, KeyError):
            return None

    # power -----------------------------------------------------------------
    @staticmethod
    def battery_watts() -> float | None:
        b = "/sys/class/power_supply/BAT0"
        pw = _int(f"{b}/power_now")
        if pw is None:
            cur, volt = _int(f"{b}/current_now"), _int(f"{b}/voltage_now")
            if cur is None or volt is None:
                return None
            pw = cur * volt / 1e6
        return pw / 1e6

    @staticmethod
    def battery_pct() -> int | None:
        return _int("/sys/class/power_supply/BAT0/capacity")

    def rapl_watts(self) -> float | None:
        """Average package power since the previous call (None the first time)."""
        try:
            r = protocol.send({"cmd": "test.rapl"}, timeout=3)
        except protocol.HubError:
            return None
        pkg = next((z for z in r.get("zones", {}).values() if z["name"].startswith("package")), None)
        if not pkg:
            return None
        now = (r["t"], pkg["uj"])
        prev, self._rapl_prev = self._rapl_prev, now
        self._rapl_max = pkg.get("max") or self._rapl_max
        if not prev or now[0] <= prev[0]:
            return None
        d = now[1] - prev[1]
        if d < 0 and self._rapl_max:
            d += self._rapl_max
        return d / 1e6 / (now[0] - prev[0])

    # sleep -------------------------------------------------------------------
    @staticmethod
    def suspend_stats() -> dict:
        base = "/sys/power/suspend_stats"
        return {k: _int(f"{base}/{k}", 0) for k in ("success", "fail", "total_hw_sleep", "last_hw_sleep")}

    # one sample for the charts and the report ------------------------------
    def sample(self) -> dict:
        s = self.sampler.sample()
        return {
            "t": round(s.t, 2), "pkg": s.pkg_temp, "ctrl": s.control_temp, "load": s.load,
            "fp": s.freq_p, "fe": s.freq_e, "fan1": s.fan1, "fan2": s.fan2,
            "gpu": s.gpu_state, "ac": s.ac_online,
            "bat_w": (s.battery or {}).get("watts") if s.ac_online is False else None,
        }


# -- per-process CPU ---------------------------------------------------------------

class ProcTracker:
    """CPU time per process between two snapshots (user-visible processes;
    root-owned ones are included too, /proc/<pid>/stat is world-readable)."""

    HZ = os.sysconf("SC_CLK_TCK")

    def __init__(self):
        self.t0 = time.monotonic()
        self.snap0 = self._snap()

    @staticmethod
    def _snap() -> dict[int, tuple[int, str]]:
        out = {}
        for p in Path("/proc").glob("[0-9]*"):
            try:
                st = (p / "stat").read_text()
                f = st[st.rindex(")") + 2:].split()
                comm = st[st.index("(") + 1:st.rindex(")")]
                out[int(p.name)] = (int(f[11]) + int(f[12]), comm)
            except (OSError, ValueError, IndexError):
                pass
        return out

    def top(self, n: int = 12) -> tuple[float, list[dict]]:
        """(total % of one core, [{pid, comm, cpu %}]) since construction."""
        secs = max(0.5, time.monotonic() - self.t0)
        now = self._snap()
        rows = []
        for pid, (ticks, comm) in now.items():
            d = ticks - self.snap0.get(pid, (0, comm))[0]
            if d > 0:
                rows.append({"pid": pid, "comm": comm, "cpu": 100.0 * d / self.HZ / secs})
        rows.sort(key=lambda r: -r["cpu"])
        total = sum(r["cpu"] for r in rows)
        return total, [{**r, "cpu": round(r["cpu"], 1)} for r in rows[:n]]


def proc_cpu(names: tuple[str, ...]) -> dict[str, int]:
    """Cumulative CPU ticks of the first process with each name."""
    out = {}
    for n in names:
        pid = run(["pidof", "-s", n])
        if pid.isdigit():
            try:
                st = Path(f"/proc/{pid}/stat").read_text()
                f = st[st.rindex(")") + 2:].split()
                out[n] = int(f[11]) + int(f[12])
            except (OSError, ValueError, IndexError):
                pass
    return out


# -- background load ---------------------------------------------------------------

class Load:
    """stress-ng (CPU) or gpu_burn (GPU) for a bounded time; always killed."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def cpu(self, seconds: int) -> bool:
        exe = shutil.which("stress-ng")
        if exe:
            argv = [exe, "--cpu", "0", "--cpu-method", "matrixprod", "--timeout", f"{seconds}s", "--quiet"]
        else:      # fallback: one busy python per CPU
            n = os.cpu_count() or 4
            argv = ["python3", "-c",
                    f"import multiprocessing as m,time\n"
                    f"def b():\n t=time.time()+{seconds}\n while time.time()<t: pass\n"
                    f"ps=[m.Process(target=b) for _ in range({n})]\n[p.start() for p in ps]\n[p.join() for p in ps]"]
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        return True

    def gpu(self, seconds: int) -> bool:
        if not (GPU_BURN.is_file() and os.access(GPU_BURN, os.X_OK)):
            return False
        # gpu_burn looks for compare.ptx in its working directory
        self.proc = subprocess.Popen([str(GPU_BURN), str(seconds)], cwd=GPU_BURN.parent,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
        return True

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, 15)
                self.proc.wait(5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, 9)
                except OSError:
                    pass
        self.proc = None


# -- system info for the report header ------------------------------------------------

def pkg_versions(names: list[str]) -> dict[str, str]:
    out = run(["pacman", "-Q", *names])
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def autocpufreq_summary() -> dict[str, dict]:
    from . import cpuconf
    try:
        conf = cpuconf.parse(paths.AUTOCPUFREQ_CONF.read_text())
    except OSError:
        return {}
    keep = ("governor", "energy_performance_preference", "turbo", "platform_profile",
            "scaling_min_freq", "scaling_max_freq", "enforce_platform_profile")
    return {sec: {k: v for k, v in vals.items() if k in keep} for sec, vals in conf.items()}


def system_info() -> dict:
    nv = _read("/proc/driver/nvidia/version", "") or ""
    m = re.search(r"Module for \S+\s+([\d.]+)", nv)
    hpwmi = _read("/usr/src/hp-wmi-omen-1.0/hp-wmi.c", "") or ""
    fan = {}
    try:
        fan = json.loads(paths.FAN_CONF.read_text())
    except (OSError, ValueError):
        pass
    units = run(["systemctl", "--user", "is-active", "plasmashell-dpms-guard.service"])
    return {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "machine": f"{_read('/sys/class/dmi/id/product_name')} (board {_read('/sys/class/dmi/id/board_name')})",
        "bios": f"{_read('/sys/class/dmi/id/bios_version')} {_read('/sys/class/dmi/id/bios_date')}",
        "kernel": platform.release(),
        "cmdline": _read("/proc/cmdline"),
        "nvidia_driver": m.group(1) if m else None,
        "packages": pkg_versions(["kwin", "plasma-workspace", "qt6-base", "mesa", "nvidia-open",
                                  "auto-cpufreq", "powertop"]),
        "hp_wmi_patch_skip_same_profile": "already active, skipping" in hpwmi,
        "auto_cpufreq": autocpufreq_summary(),
        "platform_profile": _read("/sys/firmware/acpi/platform_profile"),
        "fans": {k: fan.get(k) for k in ("mode", "active_curve", "temp_source", "ma_window", "spike_window")},
        "dpms_guard": units or "unknown",
        "cpus": os.cpu_count(),
    }


# -- report --------------------------------------------------------------------------

VERDICT_ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️", "skip": "⏭️"}


@dataclass
class Section:
    key: str
    title: str
    lines: list[str] = field(default_factory=list)
    verdicts: list[tuple[str, str]] = field(default_factory=list)
    data: dict = field(default_factory=dict)


class Report:
    """Markdown for people (and an AI) on top, the same numbers as JSON at
    the bottom so nothing has to be parsed out of prose."""

    def __init__(self, title: str):
        self.title = title
        self.started = dt.datetime.now()
        self.info = system_info()
        self.sections: list[Section] = []
        self.cur: Section | None = None

    def section(self, key: str, title: str) -> Section:
        self.cur = Section(key, title)
        self.sections.append(self.cur)
        return self.cur

    def line(self, text: str):
        self.cur.lines.append(text)

    def verdict(self, kind: str, text: str):
        self.cur.verdicts.append((kind, text))

    def table(self, header: list[str], rows: list[list]):
        self.line("| " + " | ".join(header) + " |")
        self.line("|" + "---|" * len(header))
        for r in rows:
            self.line("| " + " | ".join("–" if v is None else str(v) for v in r) + " |")
        self.line("")

    def put(self, key: str, value):
        self.cur.data[key] = value

    # summary for the GUI
    def counts(self) -> dict[str, int]:
        c = {"ok": 0, "warn": 0, "fail": 0}
        for s in self.sections:
            for k, _ in s.verdicts:
                if k in c:
                    c[k] += 1
        return c

    def markdown(self, cancelled: bool = False) -> str:
        i = self.info
        out = [f"# OMEN Hub · report test: {self.title}", ""]
        out += [f"- **Data:** {self.started:%d/%m/%Y %H:%M:%S}"
                + (" (test interrotto prima della fine)" if cancelled else ""),
                f"- **Macchina:** {i['machine']} · BIOS {i['bios']}",
                f"- **Kernel:** {i['kernel']} · **driver NVIDIA:** {i['nvidia_driver']}",
                f"- **Pacchetti:** " + ", ".join(f"{k} {v}" for k, v in i["packages"].items()),
                f"- **Platform profile:** {i['platform_profile']} · **hp-wmi salta i profili ripetuti:** "
                + ("sì" if i["hp_wmi_patch_skip_same_profile"] else "NO"),
                f"- **Ventole:** " + ", ".join(f"{k}={v}" for k, v in i["fans"].items()),
                f"- **Guardiano schermo spento:** {i['dpms_guard']}",
                f"- **Riga di boot:** `{i['cmdline']}`", ""]
        acf = i.get("auto_cpufreq") or {}
        if acf:
            out.append("**auto-cpufreq:**")
            out.append("")
            for sec, vals in acf.items():
                out.append(f"- [{sec}] " + ", ".join(f"{k}={v}" for k, v in vals.items()))
            out.append("")
        c = self.counts()
        out += ["## Riepilogo", "",
                f"{VERDICT_ICON['ok']} {c['ok']} ok · {VERDICT_ICON['warn']} {c['warn']} da guardare · "
                f"{VERDICT_ICON['fail']} {c['fail']} problemi", ""]
        for s in self.sections:
            for k, txt in s.verdicts:
                out.append(f"- {VERDICT_ICON.get(k, '•')} **{s.title}:** {txt}")
        out.append("")
        for s in self.sections:
            out += [f"## {s.title}", ""]
            out += s.lines
            if s.verdicts:
                out += ["", "**Esito:**", ""]
                out += [f"- {VERDICT_ICON.get(k, '•')} {t}" for k, t in s.verdicts]
            out.append("")
        out += ["## Note per chi legge (persona o AI)", "",
                "- Le potenze 'batteria' sono current_now × voltage_now: valgono solo a batteria "
                "e includono schermo e periferiche; 'CPU package' è RAPL (solo CPU).",
                "- Lo stato GPU viene da /sys/bus/pci/devices/<gpu>/power/runtime_status "
                "(suspended = D3cold, ~0 W; active = ~6-10 W anche a riposo). nvidia-smi la sveglierebbe.",
                "- Le ventole durante lo sleep si leggono nei primi campioni dopo il risveglio "
                "(campionamento 0,25 s): se lì sono a 0 RPM, durante il sonno erano ferme.",
                "- 'Pacchetto' a 100 °C per un istante è normale (hotspot di un P-core); conta la "
                "temperatura di controllo (hybrid) che usano le ventole.",
                "- Mentre il test gira, OMEN Hub e Claude Code (se aperto) consumano a loro volta "
                "un po' di CPU: compaiono nella classifica processi.", "",
                "## Dati grezzi (JSON)", "", "```json",
                json.dumps({"info": i, "sections": {s.key: s.data for s in self.sections}},
                           ensure_ascii=False, indent=1, default=str),
                "```", ""]
        return "\n".join(out)

    def save(self, cancelled: bool = False) -> Path:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        path = REPORT_DIR / f"omen-test-{self.started:%Y%m%d-%H%M%S}-{slug}.md"
        path.write_text(self.markdown(cancelled))
        latest = REPORT_DIR / "ultimo-report.md"
        try:
            latest.unlink(missing_ok=True)
            latest.symlink_to(path.name)
        except OSError:
            pass
        return path


# -- small statistics helpers ------------------------------------------------------------

def stats(values) -> dict:
    v = [x for x in values if x is not None]
    if not v:
        return {"n": 0, "avg": None, "min": None, "max": None}
    return {"n": len(v), "avg": round(sum(v) / len(v), 2), "min": round(min(v), 2), "max": round(max(v), 2)}


def fmt(v, spec="{:.1f}", unit=""):
    return "–" if v is None else spec.format(v) + (f" {unit}" if unit else "")


def journal_since(since: float, pattern: str, limit: int = 30) -> list[str]:
    out = run(["journalctl", "--no-pager", "-o", "short-iso", "--since", f"@{int(since)}"], timeout=20)
    rx = re.compile(pattern)
    hits = [re.sub(r"^\S+\s+\S+\s+", "", l) for l in out.splitlines() if rx.search(l)]
    return hits[-limit:]
