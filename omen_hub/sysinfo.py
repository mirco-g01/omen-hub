"""Live readings from sysfs/procfs, all readable as a normal user.

Two rules learned on this machine:
- nothing here may wake the dGPU: `nvidia-smi` and `lspci` pull it out of
  D3cold (that kept it at +10 W for weeks). The GPU state comes from
  power/runtime_status only; nvidia-smi is queried solely when the GPU is
  already awake *and* the caller explicitly asks for it.
- "Package id 0" is the hottest core right now, not an average: a single
  P-core at 5 GHz reads 100 °C for a second. The fan daemon (hybrid source)
  smooths it, and the estimator below mirrors that so the hub shows the
  temperature the fans actually react to.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


def _read(path, default=None):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def _read_int(path, default=None):
    raw = _read(path)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_cpulist(text: str | None) -> list[int]:
    cpus: list[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return cpus


def hwmon_by_name(name: str) -> Path | None:
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if _read(f"{d}/name") == name:
            return Path(d)
    return None


def find_nvidia_gpu() -> Path | None:
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        if _read(f"{d}/vendor") == "0x10de" and (_read(f"{d}/class") or "").startswith("0x03"):
            return Path(d)
    return None


@dataclass
class Topology:
    p_cores: list[int]
    e_cores: list[int]
    all: list[int]

    @classmethod
    def detect(cls) -> "Topology":
        allc = parse_cpulist(_read("/sys/devices/system/cpu/online"))
        p = parse_cpulist(_read("/sys/devices/cpu_core/cpus"))
        e = parse_cpulist(_read("/sys/devices/cpu_atom/cpus"))
        if not p:
            p, e = allc, []
        return cls(p, e, allc)


class TempEstimator:
    """Same idea as the fan daemon's hybrid source:
    T = max(smoothed core mean, min(package over the last spike_window samples)).
    A short spike is worth nothing; a hotspot held for ~30 s counts in full."""

    def __init__(self, ma_window=20, spike_window=15):
        self.mean_hist: deque[float] = deque(maxlen=max(1, ma_window))
        self.pkg_hist: deque[float] = deque(maxlen=max(1, spike_window))

    def push(self, package: float | None, core_mean: float | None) -> float | None:
        if core_mean is not None:
            self.mean_hist.append(core_mean)
        if package is not None:
            self.pkg_hist.append(package)
        if not self.mean_hist:
            return package
        smooth = sum(self.mean_hist) / len(self.mean_hist)
        if len(self.pkg_hist) == self.pkg_hist.maxlen:
            return max(smooth, min(self.pkg_hist))
        return smooth


@dataclass
class Snapshot:
    t: float = 0.0
    # CPU
    pkg_temp: float | None = None
    core_mean: float | None = None
    control_temp: float | None = None
    core_temps: dict = field(default_factory=dict)
    load: float | None = None          # % of all cpus
    load_p: float | None = None
    load_e: float | None = None
    freq_p: float | None = None        # MHz, mean
    freq_e: float | None = None
    freq_p_max: float | None = None
    governor: str | None = None
    epp: str | None = None
    no_turbo: bool | None = None
    pstate: str | None = None
    min_freq: int | None = None        # MHz (cpu0)
    max_freq: int | None = None
    hw_min: int | None = None
    hw_max_p: int | None = None
    hw_max_e: int | None = None
    platform_profile: str | None = None
    platform_choices: list = field(default_factory=list)
    # fans
    fan1: int | None = None
    fan2: int | None = None
    pwm: int | None = None
    pwm_enable: int | None = None
    # GPU
    gpu_path: str | None = None
    gpu_state: str | None = None       # active / suspended / ...
    gpu: dict | None = None            # nvidia-smi values when asked for
    # power
    ac_online: bool | None = None
    battery: dict | None = None
    # memory
    mem_total: int | None = None
    mem_avail: int | None = None
    swap_total: int | None = None
    swap_free: int | None = None
    nvme_temp: float | None = None


class Sampler:
    def __init__(self):
        self.topo = Topology.detect()
        self.coretemp = hwmon_by_name("coretemp")
        self.hp = hwmon_by_name("hp")
        self.nvme = hwmon_by_name("nvme")
        self.gpu_path = find_nvidia_gpu()
        self._stat_prev: dict[int, tuple[int, int]] = {}
        self.estimator = TempEstimator()
        self._core_labels = self._scan_core_labels()
        self.hw_min = _read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
        self.hw_max_p = max((_read_int(f"/sys/devices/system/cpu/cpu{c}/cpufreq/cpuinfo_max_freq", 0)
                             for c in self.topo.p_cores), default=None)
        self.hw_max_e = max((_read_int(f"/sys/devices/system/cpu/cpu{c}/cpufreq/cpuinfo_max_freq", 0)
                             for c in self.topo.e_cores), default=None)

    def configure_estimator(self, ma_window: int, spike_window: int):
        if (self.estimator.mean_hist.maxlen, self.estimator.pkg_hist.maxlen) != (ma_window, spike_window):
            self.estimator = TempEstimator(ma_window, spike_window)

    def _scan_core_labels(self):
        labels = {}
        if self.coretemp:
            for lab in self.coretemp.glob("temp*_label"):
                labels[lab.name.replace("_label", "_input")] = _read(lab)
        return labels

    # -- CPU ---------------------------------------------------------------
    def _temps(self, s: Snapshot):
        if not self.coretemp:
            return
        cores = []
        for inp, label in self._core_labels.items():
            v = _read_int(self.coretemp / inp)
            if v is None:
                continue
            c = v / 1000.0
            if label and label.startswith("Package"):
                s.pkg_temp = c
            elif label and label.startswith("Core"):
                s.core_temps[label] = c
                cores.append(c)
        if cores:
            s.core_mean = sum(cores) / len(cores)
        s.control_temp = self.estimator.push(s.pkg_temp, s.core_mean)

    def _load(self, s: Snapshot):
        cur: dict[int, tuple[int, int]] = {}
        try:
            with open("/proc/stat") as fh:
                for line in fh:
                    if not line.startswith("cpu") or line.startswith("cpu "):
                        continue
                    parts = line.split()
                    n = int(parts[0][3:])
                    vals = list(map(int, parts[1:]))
                    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                    cur[n] = (sum(vals), idle)
        except OSError:
            return
        busy: dict[int, float] = {}
        for n, (tot, idle) in cur.items():
            prev = self._stat_prev.get(n)
            if prev and tot > prev[0]:
                busy[n] = 100.0 * (1 - (idle - prev[1]) / (tot - prev[0]))
        self._stat_prev = cur
        if busy:
            s.load = sum(busy.values()) / len(busy)
            p = [busy[c] for c in self.topo.p_cores if c in busy]
            e = [busy[c] for c in self.topo.e_cores if c in busy]
            s.load_p = sum(p) / len(p) if p else None
            s.load_e = sum(e) / len(e) if e else None

    def _freq(self, s: Snapshot):
        def mhz(cpus):
            vals = [_read_int(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq") for c in cpus]
            vals = [v / 1000 for v in vals if v]
            return vals
        p, e = mhz(self.topo.p_cores), mhz(self.topo.e_cores)
        s.freq_p = sum(p) / len(p) if p else None
        s.freq_p_max = max(p) if p else None
        s.freq_e = sum(e) / len(e) if e else None
        base = "/sys/devices/system/cpu/cpu0/cpufreq"
        s.governor = _read(f"{base}/scaling_governor")
        s.epp = _read(f"{base}/energy_performance_preference")
        mn, mx = _read_int(f"{base}/scaling_min_freq"), _read_int(f"{base}/scaling_max_freq")
        s.min_freq = mn // 1000 if mn else None
        s.max_freq = mx // 1000 if mx else None
        s.hw_min = self.hw_min // 1000 if self.hw_min else None
        s.hw_max_p = self.hw_max_p // 1000 if self.hw_max_p else None
        s.hw_max_e = self.hw_max_e // 1000 if self.hw_max_e else None
        s.pstate = _read("/sys/devices/system/cpu/intel_pstate/status")
        nt = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
        s.no_turbo = None if nt is None else nt == "1"
        s.platform_profile = _read("/sys/firmware/acpi/platform_profile")
        s.platform_choices = (_read("/sys/firmware/acpi/platform_profile_choices") or "").split()

    # -- fans / GPU / power ------------------------------------------------
    def _fans(self, s: Snapshot):
        if not self.hp:
            return
        s.fan1 = _read_int(self.hp / "fan1_input")
        s.fan2 = _read_int(self.hp / "fan2_input")
        s.pwm = _read_int(self.hp / "pwm1")
        s.pwm_enable = _read_int(self.hp / "pwm1_enable")

    def _gpu(self, s: Snapshot, query_gpu: bool):
        if not self.gpu_path:
            return
        s.gpu_path = str(self.gpu_path)
        s.gpu_state = _read(self.gpu_path / "power" / "runtime_status")
        # Only ever ask nvidia-smi when the GPU is already awake: asking a
        # suspended GPU wakes it, and polling an awake one keeps it awake.
        if query_gpu and s.gpu_state == "active":
            s.gpu = nvidia_smi_query()

    def _power(self, s: Snapshot):
        ac = _read_int("/sys/class/power_supply/AC/online")
        s.ac_online = None if ac is None else ac == 1
        b = "/sys/class/power_supply/BAT0"
        if not os.path.isdir(b):
            return
        bat = {"capacity": _read_int(f"{b}/capacity"), "status": _read(f"{b}/status")}
        pw = _read_int(f"{b}/power_now")
        if pw is None:
            cur, volt = _read_int(f"{b}/current_now"), _read_int(f"{b}/voltage_now")
            if cur is not None and volt is not None:
                pw = cur * volt / 1e6
        bat["watts"] = pw / 1e6 if pw is not None else None
        now = _read_int(f"{b}/energy_now")
        full = _read_int(f"{b}/energy_full")
        design = _read_int(f"{b}/energy_full_design")
        if now is None:
            cn, cf, cd = (_read_int(f"{b}/charge_now"), _read_int(f"{b}/charge_full"),
                          _read_int(f"{b}/charge_full_design"))
            v = _read_int(f"{b}/voltage_min_design") or _read_int(f"{b}/voltage_now")
            if cn is not None and v:
                now, full = cn * v / 1e6, (cf * v / 1e6 if cf else None)
                design = cd * v / 1e6 if cd else None
        bat["wh_now"] = now / 1e6 if now else None
        bat["wh_full"] = full / 1e6 if full else None
        bat["health"] = (100.0 * full / design) if full and design else None
        if bat["watts"] and bat["watts"] > 0.3 and bat["wh_now"]:
            if bat["status"] == "Discharging":
                bat["hours_left"] = bat["wh_now"] / bat["watts"]
            elif bat["status"] == "Charging" and bat["wh_full"]:
                bat["hours_left"] = max(0.0, (bat["wh_full"] - bat["wh_now"]) / bat["watts"])
        s.battery = bat

    def _mem(self, s: Snapshot):
        info = {}
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) * 1024
        except OSError:
            return
        s.mem_total, s.mem_avail = info.get("MemTotal"), info.get("MemAvailable")
        s.swap_total, s.swap_free = info.get("SwapTotal"), info.get("SwapFree")

    def sample(self, query_gpu: bool = False) -> Snapshot:
        s = Snapshot(t=time.time())
        self._temps(s)
        self._load(s)
        self._freq(s)
        self._fans(s)
        self._gpu(s, query_gpu)
        self._power(s)
        self._mem(s)
        if self.nvme:
            v = _read_int(self.nvme / "temp1_input")
            s.nvme_temp = v / 1000 if v else None
        return s


def nvidia_smi_query() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    fields = ["temperature.gpu", "power.draw", "utilization.gpu", "clocks.gr",
              "memory.used", "memory.total", "pstate", "power.limit"]
    try:
        out = subprocess.run([exe, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    vals = [v.strip() for v in out.split("\n")[0].split(",")] if out else []
    if len(vals) != len(fields):
        return None

    def num(v):
        try:
            return float(v)
        except ValueError:
            return None
    return {"temp": num(vals[0]), "power": num(vals[1]), "util": num(vals[2]),
            "clock": num(vals[3]), "mem_used": num(vals[4]), "mem_total": num(vals[5]),
            "pstate": vals[6], "power_limit": num(vals[7])}


def kernel_cmdline() -> list[str]:
    return (_read("/proc/cmdline") or "").split()
