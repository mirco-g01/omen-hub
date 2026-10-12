"""CPU/GPU power limits and turbo ratio limits (root side, used by omen-hubd).

Measured on this machine (12/10/2026):
- RAPL PL1/PL2 (powercap, MSR and MMIO domains) are 170/170 W whatever the
  platform profile, are honoured to the watt (100 W -> 45.0 W all-core) and
  survive a platform-profile switch.
- Turbo ratio limits are programmable (PLATFORM_INFO bit 28) and honoured by
  both core types: MSR 0x1AD (P-cores, one byte per active-core bin, bin sizes
  in 0x1AE) and 0x650 (E-cores). Unlike scaling_max_freq, which the P-cores
  ignore under HWP, a x30 ratio pinned a loaded P-core to 3000 MHz.
- Each byte is the limit for "up to N active cores" (N in 0x1AE / 0x651;
  E-core bins here: 1,2,3,4,6,8,12,12). The hardware uses the lowest limit
  among the bins that apply, so the values only make sense non-increasing
  (lowering bin 1 also caps 12 loaded E-cores: measured).
- Ratios above the factory value are accepted by the MSR but NOT honoured
  (E x47 stayed at 4500 MHz, P x53 no change): overclocking would need the
  undocumented OC mailbox, deliberately not used.
- Writing those MSRs through /dev/cpu/*/msr taints the kernel
  (CPU_OUT_OF_SPEC) unless msr.allow_writes=on, so it is switched on only for
  the duration of a write and put back.
- The GPU power limit cannot be changed on this laptop ("Changing power
  management limit is not supported in current scope"); the 115 <-> 140 W
  ceiling is Dynamic Boost. Locking the graphics clock works instead and acts
  as a power cap: under gpu-burn 1970 MHz/139 W -> 1147 MHz/75.6 W with
  -lgc 210,1200. Without persistence mode the lock does not survive the GPU
  going to D3cold, so it is re-applied every time the GPU wakes up. The GPU
  is touched only when it is already awake: nvidia-smi wakes it from D3cold.

Nothing is ever raised above the firmware's own value (saved per boot).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

from . import paths

RAPL_DOMAINS = ("/sys/class/powercap/intel-rapl:0", "/sys/class/powercap/intel-rapl-mmio:0")
MSR_TRL_P, MSR_TRL_P_GROUPS, MSR_TRL_E, MSR_TRL_E_GROUPS = 0x1AD, 0x1AE, 0x650, 0x651
ALLOW_WRITES = "/sys/module/msr/parameters/allow_writes"
DEFAULTS_FILE = paths.STATE_DIR / "limits-defaults.json"
GPU_STATE_FILE = paths.STATE_DIR / "gpu-limit.json"

RATIO_RANGE = (8, 60)
PL_RANGE = (10, 250)
GPU_CLOCK_RANGE = (300, 3500)   # MHz
GPU_CLOCK_MIN = 210             # lowest graphics clock seen on this GPU


def _read(p):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def _write(p, v):
    with open(p, "w") as f:
        f.write(str(v))


def boot_id() -> str:
    return _read("/proc/sys/kernel/random/boot_id") or ""


# -- MSR --------------------------------------------------------------------------

def _cpus():
    return sorted(int(os.path.basename(p)) for p in glob.glob("/dev/cpu/[0-9]*"))


def _ensure_msr():
    if not os.path.exists("/dev/cpu/0/msr"):
        subprocess.run(["modprobe", "msr"], check=False, capture_output=True)


def rdmsr(reg: int, cpu: int = 0) -> int:
    _ensure_msr()
    fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
    try:
        return struct.unpack("<Q", os.pread(fd, 8, reg))[0]
    finally:
        os.close(fd)


def wrmsr_all(reg: int, value: int):
    _ensure_msr()
    prev = _read(ALLOW_WRITES)
    try:
        if prev not in (None, "on"):
            _write(ALLOW_WRITES, "on")
        for c in _cpus():
            fd = os.open(f"/dev/cpu/{c}/msr", os.O_WRONLY)
            try:
                os.pwrite(fd, struct.pack("<Q", value), reg)
            finally:
                os.close(fd)
    finally:
        if prev not in (None, "on"):
            _write(ALLOW_WRITES, prev)


def unpack_ratios(v: int) -> list[int]:
    return [(v >> (8 * i)) & 0xFF for i in range(8)]


def pack_ratios(r: list[int]) -> int:
    out = 0
    for i, x in enumerate(r):
        out |= (int(x) & 0xFF) << (8 * i)
    return out


# -- defaults (firmware values for this boot) ----------------------------------------

def read_pl() -> dict:
    """{domain: (pl1_W, pl2_W)}"""
    out = {}
    for d in RAPL_DOMAINS:
        a, b = _read(f"{d}/constraint_0_power_limit_uw"), _read(f"{d}/constraint_1_power_limit_uw")
        if a is not None and b is not None:
            out[d] = (int(a) // 10**6, int(b) // 10**6)
    return out


def _gpu_dev() -> Path | None:
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        if _read(f"{d}/vendor") == "0x10de" and (_read(f"{d}/class") or "").startswith("0x03"):
            return Path(d)
    return None


def gpu_awake() -> bool:
    d = _gpu_dev()
    return bool(d) and _read(d / "power" / "runtime_status") == "active"


def gpu_query() -> dict | None:
    """Only when awake; never call this on a suspended GPU."""
    exe = shutil.which("nvidia-smi")
    if not exe or not gpu_awake():
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=enforced.power.limit,power.default_limit,"
                              "power.min_limit,power.max_limit,clocks.max.graphics",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        vals = [float(x) for x in out.split("\n")[0].split(",")]
        return {"limit": vals[0], "default": vals[1], "min": vals[2], "max": vals[3],
                "clock_max": vals[4]}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def load_defaults() -> dict:
    """Firmware values captured the first time in this boot (MSRs and RAPL go
    back to them at every boot, and the hub may have changed them since)."""
    try:
        d = json.loads(DEFAULTS_FILE.read_text())
        if d.get("boot_id") == boot_id():
            if d.get("trl_e_groups") is None:
                try:
                    d["trl_e_groups"] = unpack_ratios(rdmsr(MSR_TRL_E_GROUPS))
                    save_defaults(d)
                except OSError:
                    pass
            if d.get("gpu") is None and gpu_awake():
                d["gpu"] = gpu_query()
                save_defaults(d)
            return d
    except (OSError, ValueError):
        pass
    d = {"boot_id": boot_id(), "pl": None, "trl_p": None, "trl_p_groups": None, "trl_e": None, "gpu": None}
    pl = read_pl()
    if pl:
        d["pl"] = list(next(iter(pl.values())))
    try:
        d["trl_p"] = unpack_ratios(rdmsr(MSR_TRL_P))
        d["trl_p_groups"] = unpack_ratios(rdmsr(MSR_TRL_P_GROUPS))
        d["trl_e"] = unpack_ratios(rdmsr(MSR_TRL_E))
        d["trl_e_groups"] = unpack_ratios(rdmsr(MSR_TRL_E_GROUPS))
    except OSError:
        pass
    if gpu_awake():
        d["gpu"] = gpu_query()
    save_defaults(d)
    return d


def save_defaults(d: dict):
    try:
        paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULTS_FILE.write_text(json.dumps(d) + "\n")
    except OSError:
        pass


# -- desired state ----------------------------------------------------------------------

def desired(profile: dict, defaults: dict) -> dict:
    """Clamp the profile's requests to the firmware values."""
    out = {}
    dpl = defaults.get("pl") or [None, None]
    for i, key in enumerate(("pl1", "pl2")):
        v, fw = profile.get(key), dpl[i]
        out[key] = fw if v is None or fw is None else min(int(v), fw)
    if out.get("pl1") and out.get("pl2") and out["pl2"] < out["pl1"]:
        out["pl2"] = out["pl1"]
    for key, field in (("trl_p", "ratio_p"), ("trl_e", "ratio_e")):
        fw = defaults.get(key)
        if not fw:
            continue
        req = profile.get(field)
        if req is None:
            out[key] = list(fw)
            continue
        req = [req] * 8 if isinstance(req, int) else list(req)
        vals = [f if r is None else min(int(r), f) for r, f in zip(req, fw)]
        # the hardware applies the lowest limit among the bins that apply:
        # keep the list non-increasing so what is written is what happens
        for i in range(1, len(vals)):
            vals[i] = min(vals[i], vals[i - 1])
        out[key] = vals
    out["gpu_clock"] = profile.get("gpu_clock")
    return out


# -- enforcement ------------------------------------------------------------------------------

APPLIED_FILE = paths.STATE_DIR / "limits-applied.json"
_gpu_was_awake: bool | None = None


def _applied() -> set:
    try:
        d = json.loads(APPLIED_FILE.read_text())
        return set(d.get("keys", [])) if d.get("boot_id") == boot_id() else set()
    except (OSError, ValueError):
        return set()


def _save_applied(keys: set):
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    APPLIED_FILE.write_text(json.dumps({"boot_id": boot_id(), "keys": sorted(keys)}) + "\n")


def enforce(profile: dict, periodic: bool = False) -> list[str]:
    """Bring hardware to what ``profile`` asks; returns what changed.

    Only what the hub itself set is ever touched: an empty field restores the
    firmware value once (if the hub had changed it) and is then left alone,
    so whatever the firmware does on its own is not fought.
    ``periodic`` (the 4 s watcher) skips the GPU unless it just woke up."""
    global _gpu_was_awake
    if paths.DRY_RUN:
        return []
    msgs = []
    defaults = load_defaults()
    want = desired(profile, defaults)
    applied = _applied()
    before = set(applied)

    pl_set = profile.get("pl1") is not None or profile.get("pl2") is not None
    if (pl_set or "pl" in applied) and want.get("pl1") is not None:
        for d, (pl1, pl2) in read_pl().items():
            if (pl1, pl2) == (want["pl1"], want["pl2"]):
                continue
            try:
                order = (("constraint_1", want["pl2"]), ("constraint_0", want["pl1"]))
                if want["pl1"] > pl1:
                    order = order[::-1]
                for c, v in order:
                    _write(f"{d}/{c}_power_limit_uw", int(v) * 10**6)
                if d == RAPL_DOMAINS[0]:
                    msgs.append(f"CPU PL1/PL2 {want['pl1']}/{want['pl2']} W"
                                + ("" if pl_set else " (firmware)"))
            except OSError as exc:
                msgs.append(f"RAPL {d}: {exc}")
        applied.add("pl") if pl_set else applied.discard("pl")

    for reg, key, field, label in ((MSR_TRL_P, "trl_p", "ratio_p", "P-core"),
                                   (MSR_TRL_E, "trl_e", "ratio_e", "E-core")):
        is_set = profile.get(field) is not None
        if not want.get(key) or not (is_set or key in applied):
            continue
        try:
            if unpack_ratios(rdmsr(reg)) != want[key]:
                wrmsr_all(reg, pack_ratios(want[key]))
                msgs.append(f"moltiplicatore {label} ×{max(want[key])}"
                            + ("" if len(set(want[key])) == 1 else f"–×{min(want[key])}")
                            + ("" if is_set else " (firmware)"))
            applied.add(key) if is_set else applied.discard(key)
        except OSError as exc:
            msgs.append(f"MSR {label}: {exc}")

    if applied != before:
        _save_applied(applied)

    awake = gpu_awake()
    woke = awake and _gpu_was_awake is not True     # first check counts as a wake-up
    _gpu_was_awake = awake
    if not periodic or woke:
        if woke:
            time.sleep(2)       # let the driver finish bringing it up
        msg = enforce_gpu(want.get("gpu_clock"), periodic and woke)
        if msg:
            msgs.append(msg)
    return msgs


def enforce_gpu(clock: int | None, rewake: bool = False) -> str | None:
    """Lock (or release) the GPU graphics clock, only if the GPU is awake;
    otherwise the watcher does it when the GPU wakes up. The lock cannot be
    read back, so what was applied is remembered in GPU_STATE_FILE."""
    try:
        state = json.loads(GPU_STATE_FILE.read_text())
        if state.get("boot_id") != boot_id():
            state = {}
    except (OSError, ValueError):
        state = {}
    applied = state.get("clock")
    if clock == applied and not (rewake and clock):
        return None
    if not gpu_awake():
        return None
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    if clock:
        p = subprocess.run([exe, "-lgc", f"{GPU_CLOCK_MIN},{int(clock)}"], capture_output=True,
                           text=True, timeout=10)
        ok = "GPU clocks set" in p.stdout
    else:
        p = subprocess.run([exe, "-rgc"], capture_output=True, text=True, timeout=10)
        ok = p.returncode == 0 and "not supported" not in (p.stdout + p.stderr)
    if not ok:
        return f"GPU: {(p.stdout + p.stderr).strip().splitlines()[0] if (p.stdout + p.stderr).strip() else 'errore'}"
    paths.STATE_DIR.mkdir(parents=True, exist_ok=True)
    GPU_STATE_FILE.write_text(json.dumps({"boot_id": boot_id(), "clock": clock}) + "\n")
    if rewake and clock == applied:
        return None          # silent re-apply after a wake-up
    return f"GPU clock max {clock} MHz" if clock else "GPU clock libero"


def info() -> dict:
    """For the GUI: firmware values and what is in force now."""
    d = load_defaults()
    cur = {}
    try:
        cur["trl_p"] = unpack_ratios(rdmsr(MSR_TRL_P))
        cur["trl_e"] = unpack_ratios(rdmsr(MSR_TRL_E))
    except OSError:
        pass
    pl = read_pl()
    if pl:
        cur["pl"] = list(next(iter(pl.values())))
    return {"defaults": d, "current": cur,
            "p_bins": d.get("trl_p_groups") or list(range(1, 9)),
            "e_bins": d.get("trl_e_groups") or list(range(1, 9))}
