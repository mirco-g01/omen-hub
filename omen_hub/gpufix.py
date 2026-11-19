"""NVIDIA runtime D3 (RTD3) on kernel >= 7.3: detect, patch, rebuild.

Since kernel 7.3 kthreads have an empty root (nullfs). The NVIDIA driver
(615 and later) probes the GPU in the "nv-probe" kthread during module init,
where filp_open("/sys/bus/pci/devices/<gpu>/config") fails with -ENOENT. The
driver concludes runtime PM is unavailable, /proc/driver/nvidia/gpus/*/power
says "Runtime D3 status: Not supported", and the dGPU never reaches D3cold:
17.8 W at idle on battery instead of 11.6 W (OMEN Hub test of 18/11/2026).

The fix is the CachyOS patch kernel-patches/7.3/misc/nvidia/0004 ("Open
sysfs config from the userspace root on 7.3+"): that filp_open runs inside
scoped_with_init_fs(). It can only go into a driver built from source, so the
prebuilt nvidia-open is swapped for nvidia-open-dkms, and the patch has to be
applied again whenever nvidia-open-dkms ships a new source tree (until NVIDIA
includes the fix: then the source already has scoped_with_init_fs and nothing
is patched).

Everything here only reads, except apply(), which omen-hubd runs as root. No
probe wakes the GPU: the NVIDIA procfs power file is read only when
runtime_status already says "active".
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import paths

NEEDS_KERNEL = (7, 3)
PREBUILT, DKMS = "nvidia-open", "nvidia-open-dkms"

STATE = paths.STATE_DIR / "gpufix.json"            # last apply (root writes, all read)
CHECK = paths.STATE_DIR / "gpufix-check.json"      # rewritten by the pacman hook: wakes the notifier
JOB_LOCK = paths.P("/run/omen-hub/gpufix.job")      # pid of omen-hubd while an apply runs
PACMAN_LOCK = Path("/var/lib/pacman/db.lck")

# -- the patch ------------------------------------------------------------------------------------
# Same change as the CachyOS patch, applied by anchors instead of by `patch`:
# each anchor must occur exactly once, or nothing is written.

INCLUDE_ANCHOR = "#include <linux/kthread.h>\n"
INCLUDE_ADD = "#include <linux/fs_struct.h>\n#include <linux/version.h>\n"
OPEN_ANCHOR = "        struct file *file = filp_open(filename, O_RDONLY, 0);\n"
OPEN_FIXED = """\
        struct file *file;

#if LINUX_VERSION_CODE >= KERNEL_VERSION(7, 3, 0)
        /*
         * On 7.3+ kthreads have an empty root, and probe can run in the
         * nv-probe kthread. Look the path up from the userspace root.
         */
        scoped_with_init_fs()
            file = filp_open(filename, O_RDONLY, 0);
#else
        file = filp_open(filename, O_RDONLY, 0);
#endif
"""
OUR_MARK = "On 7.3+ kthreads have an empty root"
UPSTREAM_MARK = "scoped_with_init_fs"
PATCH_ORIGIN = ("CachyOS kernel-patches 7.3/misc/nvidia/"
                "0004-Open-sysfs-config-from-the-userspace-root-on-7.3.patch")


class GpuFixError(RuntimeError):
    pass


def source_state(text: str) -> str:
    """'patched' (by us) | 'upstream' (NVIDIA fixed it) | 'unfixed'."""
    if OUR_MARK in text:
        return "patched"
    if UPSTREAM_MARK in text:
        return "upstream"
    return "unfixed"


def patch_text(text: str) -> str:
    if source_state(text) != "unfixed":
        return text
    for anchor, what in ((INCLUDE_ANCHOR, "include di kthread.h"), (OPEN_ANCHOR, "filp_open del config sysfs")):
        n = text.count(anchor)
        if n != 1:
            raise GpuFixError(f"nv-pci.c è cambiato ({what}: {n} occorrenze invece di 1): "
                              "la patch va aggiornata per questa versione del driver")
    text = text.replace(INCLUDE_ANCHOR, INCLUDE_ANCHOR + INCLUDE_ADD)
    return text.replace(OPEN_ANCHOR, OPEN_FIXED)


# -- facts ------------------------------------------------------------------------------------------

def kernel_tuple(release: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", release)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _read(path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _out(argv: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


@dataclass
class Facts:
    kernel: str = ""                       # running
    kernels: list[str] = field(default_factory=list)   # installed, with headers (next boot)
    gpu: str | None = None                # sysfs path of the NVIDIA GPU
    runtime_status: str | None = None      # active | suspended
    rtd3: str | None = None                # "Runtime D3 status" (read only when active)
    regkey: int | None = None              # NVreg_DynamicPowerManagement in force
    pkg: str | None = None                 # nvidia-open | nvidia-open-dkms | other driver package
    ver: str | None = None                 # driver version, no epoch/pkgrel
    src_file: str | None = None            # nv-pci.c of the DKMS source
    src_state: str | None = None           # patched | upstream | unfixed
    dkms_mod: str | None = None
    dkms_built: bool = False               # installed by DKMS for every kernel in `kernels`
    module_mtime: float | None = None      # nvidia.ko that the next modprobe would load
    boot_time: float = 0.0
    applied_at: float | None = None
    job_running: bool = False


def find_gpu() -> Path | None:
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        if _read(f"{d}/vendor") == "0x10de" and (_read(f"{d}/class") or "").startswith("0x03"):
            return Path(d)
    return None


def rtd3_status(gpu: Path | None) -> tuple[str | None, str | None]:
    """(runtime_status, Runtime D3 status). The procfs file goes through the
    driver: it is read only when the GPU is already awake."""
    if gpu is None:
        return None, None
    st = _read(gpu / "power" / "runtime_status")
    if st != "active":
        return st, None
    for f in glob.glob("/proc/driver/nvidia/gpus/*/power"):
        for line in (_read(f) or "").splitlines():
            if line.startswith("Runtime D3 status:"):
                return st, line.split(":", 1)[1].strip()
    return st, None


def regkey() -> int | None:
    for line in (_read("/proc/driver/nvidia/params") or "").splitlines():
        if line.startswith("DynamicPowerManagement:"):
            try:
                return int(line.split(":", 1)[1].strip(), 0)
            except ValueError:
                return None
    return None


def driver_package() -> tuple[str | None, str | None]:
    out = _out(["pacman", "-Q", DKMS, PREBUILT, "nvidia", "nvidia-dkms"])
    found = dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)
    for name in (DKMS, PREBUILT, "nvidia-dkms", "nvidia"):
        if name in found:
            ver = re.sub(r"^\d+:", "", found[name]).rsplit("-", 1)[0]
            return name, ver
    return None, None


def dkms_source(ver: str) -> tuple[Path | None, str | None]:
    """(nv-pci.c, DKMS module name) of /usr/src/nvidia-<ver>."""
    src = paths.P(f"/usr/src/nvidia-{ver}")
    conf = _read(src / "dkms.conf") or ""
    m = re.search(r'^PACKAGE_NAME="?([^"\n]+)"?', conf, re.M)
    hit = next((p for p in (src / "kernel-open/nvidia/nv-pci.c", src / "nvidia/nv-pci.c") if p.is_file()), None)
    if hit is None and src.is_dir():           # another layout: search (slow, thousands of files)
        hit = next((p for p in sorted(src.rglob("nv-pci.c")) if p.parent.name == "nvidia"), None)
    return hit, (m.group(1) if m else None)


def kernels_with_headers() -> list[str]:
    """Installed kernels DKMS can build for. After a kernel upgrade the running
    kernel's module tree is gone: what counts is what the next boot loads."""
    out = []
    for d in sorted(glob.glob("/usr/lib/modules/*")):
        if Path(d, "build").exists() and Path(d, "vmlinuz").exists():
            out.append(Path(d).name)
    return out


def dkms_built_after(src: Path, kernels: list[str]) -> bool:
    """True when every kernel has a DKMS nvidia.ko newer than nv-pci.c, i.e.
    built from the source as it is now. `dkms status` would say "installed"
    for a module built before the patch too (and takes most of a second)."""
    try:
        since = src.stat().st_mtime
    except OSError:
        return False
    for k in kernels:
        mods = glob.glob(f"/usr/lib/modules/{k}/updates/dkms/nvidia.ko*")
        if not mods or max(os.stat(m).st_mtime for m in mods) < since:
            return False
    return bool(kernels)


def boot_time() -> float:
    for line in (_read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def job_running() -> bool:
    pid = _read(JOB_LOCK)
    return bool(pid and pid.isdigit() and Path(f"/proc/{pid}").exists())


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def gather() -> Facts:
    f = Facts(kernel=os.uname().release, boot_time=boot_time(), job_running=job_running())
    f.kernels = kernels_with_headers() or [f.kernel]
    gpu = find_gpu()
    f.gpu = str(gpu) if gpu else None
    f.runtime_status, f.rtd3 = rtd3_status(gpu)
    f.regkey = regkey()
    f.pkg, f.ver = driver_package()
    if f.pkg == DKMS and f.ver:
        src, f.dkms_mod = dkms_source(f.ver)
        if src:
            f.src_file = str(src)
            f.src_state = source_state(_read(src) or "")
            f.dkms_built = dkms_built_after(src, f.kernels)
    mod = _out(["modinfo", "-n", "nvidia"]).strip()
    if mod:
        try:
            f.module_mtime = os.stat(mod).st_mtime
        except OSError:
            pass
    f.applied_at = load_state().get("applied_at")
    return f


# -- verdict ---------------------------------------------------------------------------------------

@dataclass
class Status:
    code: str        # na | ok | needed | lost | reboot | ineffective | running | other
    title: str
    detail: str
    action: str | None = None        # apply | reboot
    facts: dict = field(default_factory=dict)

    @property
    def needs_attention(self) -> bool:
        return self.code in ("needed", "lost", "reboot", "ineffective", "running")

    def signature(self) -> str:
        """What a notification was about: a new driver or kernel is news again."""
        f = self.facts
        return f"{self.code}|{f.get('pkg')}|{f.get('ver')}|{f.get('kernel')}"

    def as_dict(self) -> dict:
        return asdict(self)


WASTE = "~6 W in più a riposo"


def verdict(f: Facts) -> Status:
    facts = asdict(f)
    S = lambda code, title, detail, action=None: Status(code, title, detail, action, facts)  # noqa: E731
    if f.job_running:
        return S("running", "Correzione GPU in corso",
                 "omen-hubd sta applicando la correzione RTD3 e ricompilando il driver NVIDIA.")
    if not f.gpu or not f.pkg:
        return S("na", "Nessun driver NVIDIA", "Nessuna GPU NVIDIA o nessun pacchetto del driver installato.")
    broken = (f.runtime_status == "active" and f.rtd3 is not None
              and not f.rtd3.startswith("Enabled") and f.regkey != 0)
    works = f.runtime_status == "suspended" or (f.rtd3 or "").startswith("Enabled")
    if f.regkey == 0:
        return S("na", "RTD3 disattivato di proposito",
                 "NVreg_DynamicPowerManagement=0: il driver non prova nemmeno a spegnere la GPU.")
    if kernel_tuple(f.kernel) < NEEDS_KERNEL:
        if broken:
            return S("other", "RTD3 non attivo",
                     f"Runtime D3 status: {f.rtd3}, ma con il kernel {f.kernel} non è il bug del 7.3.")
        return S("na", "Correzione non necessaria", f"Kernel {f.kernel}: il bug riguarda il 7.3 e successivi.")
    if f.pkg not in (PREBUILT, DKMS):
        return S("other" if broken else "na", "Driver non gestito",
                 f"{f.pkg} {f.ver}: la correzione automatica copre solo nvidia-open.")
    fixed_src = f.pkg == DKMS and f.src_state in ("patched", "upstream") and f.dkms_built
    by = "di NVIDIA" if f.src_state == "upstream" else "patch CachyOS"
    kernels = ", ".join(f.kernels) or f.kernel
    if broken:
        if fixed_src:
            newer = max(f.module_mtime or 0, f.applied_at or 0) > f.boot_time
            if newer:
                return S("reboot", "Riavvia per spegnere la GPU a riposo",
                         f"Il driver {f.ver} è corretto ({by}) e compilato per {kernels}: "
                         "il modulo caricato adesso è ancora quello vecchio.", "reboot")
            return S("ineffective", "La correzione non ha effetto",
                     f"Il sorgente {f.ver} è corretto ({by}) e il modulo è compilato, ma dopo il riavvio "
                     f"il driver dice ancora «{f.rtd3}». Riapplica la correzione o guarda il log di DKMS.",
                     "apply")
        if f.pkg == PREBUILT:
            how = (f"nvidia-open {f.ver} è precompilato e non si può correggere: l'hub lo sostituisce con "
                   "nvidia-open-dkms, applica la patch e ricompila (5–10 minuti).")
        elif f.src_state == "unfixed":
            how = f"Il sorgente di nvidia-open-dkms {f.ver} non ha la correzione: l'hub la applica e ricompila."
        elif f.src_state:
            how = f"Il sorgente {f.ver} è corretto ma il modulo per {kernels} non è compilato: l'hub lo ricompila."
        else:
            how = f"Il sorgente DKMS di nvidia {f.ver} non si trova in /usr/src: reinstalla nvidia-open-dkms."
        return S("needed", "La GPU NVIDIA non si spegne a riposo",
                 f"Il driver dice «Runtime D3 status: {f.rtd3}» (bug kernel 7.3 + nvidia-open): "
                 f"{WASTE}. {how}", "apply")
    if works and f.pkg == DKMS and not fixed_src:
        what = ("il nuovo sorgente non ha la correzione" if f.src_state == "unfixed"
                else f"il modulo per {kernels} non è compilato")
        return S("lost", "Correzione GPU persa con l'aggiornamento",
                 f"nvidia-open-dkms {f.ver}: {what}. Adesso la GPU si spegne ancora, ma dal prossimo avvio "
                 f"resterà accesa ({WASTE}).", "apply")
    if works:
        src = {"patched": " (patch CachyOS applicata dall'hub)", "upstream": " (correzione inclusa da NVIDIA)"}
        return S("ok", "RTD3 attivo", f"La GPU può spegnersi da sola a riposo{src.get(f.src_state or '', '')}.")
    return S("na", "Stato RTD3 sconosciuto",
             "GPU accesa ma il driver NVIDIA non espone lo stato RTD3 (modulo non caricato?).")


def status() -> Status:
    return verdict(gather())


def explain_for_report(st: Status | None = None) -> str | None:
    """One line for a test report when the GPU stayed awake."""
    st = st or status()
    if st.code in ("needed", "reboot", "ineffective", "lost"):
        return f"{st.title}: {st.detail}"
    return None


# -- apply (omen-hubd, root) -----------------------------------------------------------------------

def initramfs_has_nvidia() -> bool:
    files = [paths.P("/etc/mkinitcpio.conf"), *sorted(paths.P("/etc/mkinitcpio.conf.d").glob("*.conf"))]
    for f in files:
        for line in (_read(f) or "").splitlines():
            if not line.lstrip().startswith("#") and "MODULES" in line and "nvidia" in line:
                return True
    return False


Run = Callable[..., tuple[int, str]]


def apply(run: Run, write: Callable[[Path, str], None], step: Callable[[str], None],
          dry_run: bool = False) -> str:
    """Swap to nvidia-open-dkms if needed, patch nv-pci.c, rebuild for every
    installed kernel, regenerate the initramfs. Raises GpuFixError."""
    f = gather()
    if kernel_tuple(f.kernel) < NEEDS_KERNEL:
        raise GpuFixError(f"kernel {f.kernel}: la correzione serve dal 7.3 in poi")
    if f.pkg not in (PREBUILT, DKMS):
        raise GpuFixError(f"driver {f.pkg or 'assente'}: la correzione copre solo nvidia-open")
    if PACMAN_LOCK.exists() and not dry_run:
        raise GpuFixError("pacman è in uso (db.lck presente): riprova quando ha finito")

    if f.pkg == PREBUILT:
        step("Sostituisco nvidia-open con nvidia-open-dkms (download e prima compilazione)…")
        # --ask=4 answers yes to "nvidia-open and nvidia-open-dkms are in conflict. Remove?"
        rc, out = run(["pacman", "-S", "--noconfirm", "--ask=4", "--needed", DKMS], timeout=2400)
        if rc != 0:
            raise GpuFixError(f"pacman -S {DKMS} fallito (rc={rc}): {out[-800:]}")
        f = gather()
        if f.pkg != DKMS and not dry_run:
            raise GpuFixError(f"{DKMS} non risulta installato dopo pacman")
    if dry_run and f.pkg != DKMS:
        return "(dry run) da qui in poi servirebbe nvidia-open-dkms installato"

    src = Path(f.src_file) if f.src_file else None
    if src is None or not f.dkms_mod:
        raise GpuFixError(f"sorgente DKMS di nvidia {f.ver} non trovato in /usr/src")
    text = src.read_text()
    state = source_state(text)
    if state == "unfixed":
        step(f"Applico la patch a {src}…")
        write(src, patch_text(text))
    else:
        step("Il sorgente ha già la correzione " + ("(patch dell'hub)." if state == "patched" else "di NVIDIA."))

    kernels = f.kernels
    target = f"{f.dkms_mod}/{f.ver}"
    for k in kernels:
        step(f"Compilo il modulo NVIDIA per {k} (qualche minuto)…")
        rc, out = run(["dkms", "build", "--force", target, "-k", k], timeout=2400)
        if rc != 0:
            raise GpuFixError(f"dkms build {target} -k {k} fallito (rc={rc}): {out[-1200:]}")
        rc, out = run(["dkms", "install", "--force", target, "-k", k], timeout=900)
        if rc != 0:
            raise GpuFixError(f"dkms install {target} -k {k} fallito (rc={rc}): {out[-1200:]}")
    if initramfs_has_nvidia():
        step("Rigenero l'initramfs (i moduli nvidia sono in MODULES)…")
        rc, out = run(["mkinitcpio", "-P"], timeout=900)
        if rc != 0:
            raise GpuFixError(f"mkinitcpio -P fallito (rc={rc}): {out[-1200:]}")
    write(STATE, json.dumps({"applied_at": time.time(), "version": f.ver, "kernels": kernels,
                             "source_state": source_state(src.read_text()) if not dry_run else state,
                             "patch": PATCH_ORIGIN}, indent=2) + "\n")
    return f"nvidia {f.ver} corretto e compilato per {', '.join(kernels)}: riavvia per attivarlo."


def write_check(st: Status) -> None:
    """Touch the file the session notifier watches (omen-hub-notify.path)."""
    CHECK.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECK.with_name(f".{CHECK.name}.tmp")
    tmp.write_text(json.dumps({"t": time.time(), "code": st.code, "signature": st.signature()}) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, CHECK)
