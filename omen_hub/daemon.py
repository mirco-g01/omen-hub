"""omen-hubd: the only part of the hub that runs as root.

It accepts a short whitelist of requests on a unix socket and refuses
everything else: no shell, no arbitrary paths, no arbitrary units. Callers
must be root or in the admin group (wheel by default), checked with
SO_PEERCRED on every connection.

Every file it rewrites is backed up first under /var/lib/omen-hub/backup.
"""

from __future__ import annotations

import datetime as dt
import grp
import json
import os
import pwd
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import __version__, components, cpuconf, fanconf, group, limits, paths, profiles

MAX_REQUEST = 1 << 20


def log(msg: str):
    print(msg, flush=True)


# -- helpers -------------------------------------------------------------------

def backup(path: Path, keep: int = 15):
    if not path.exists():
        return None
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = paths.BACKUP_DIR / f"{path.name}.{stamp}"
    shutil.copy2(path, dest)
    old = sorted(paths.BACKUP_DIR.glob(f"{path.name}.*"))
    for f in old[:-keep]:
        f.unlink(missing_ok=True)
    return dest


def write_atomic(path: Path, text: str, mode: int = 0o644, keep_backup: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if keep_backup:
        backup(path)
    tmp = path.with_name(f".{path.name}.omen-hub.tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(argv: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    if paths.DRY_RUN:
        log("DRY-RUN: " + " ".join(argv))
        return 0, "(dry run) " + " ".join(argv)
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin", "LC_ALL": "C.UTF-8",
           "HOME": "/root"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"timeout dopo {timeout} s"
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def systemctl(*args, timeout=60) -> tuple[int, str]:
    return run(["systemctl", *args], timeout=timeout)


def read_sys(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def platform_choices() -> list[str]:
    return (read_sys("/sys/firmware/acpi/platform_profile_choices") or "").split()


def freq_range() -> tuple[int, int] | None:
    lo = read_sys("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
    his = [read_sys(f"/sys/devices/system/cpu/cpu{n}/cpufreq/cpuinfo_max_freq")
           for n in range(os.cpu_count() or 1)]
    his = [int(h) for h in his if h]
    if not lo or not his:
        return None
    return int(lo) // 1000, max(his) // 1000


def on_ac() -> bool | None:
    v = read_sys("/sys/class/power_supply/AC/online")
    return None if v is None else v == "1"


def load_fan_config() -> dict:
    try:
        return json.loads(paths.FAN_CONF.read_text())
    except (OSError, ValueError):
        return {}


# -- the hub -------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.conf = self._load_conf()
        self.allowed_gid = self._group_id(self.conf.get("admin_group", "wheel"))

    @staticmethod
    def _load_conf() -> dict:
        try:
            return json.loads(paths.HUBD_CONF.read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _group_id(name):
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    def authorized(self, uid: int, gid: int) -> bool:
        if uid == 0:
            return True
        if uid in self.conf.get("allowed_uids", []):
            return True
        if self.allowed_gid is None:
            return False
        if gid == self.allowed_gid:
            return True
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            return False
        return user in grp.getgrgid(self.allowed_gid).gr_mem

    def handle(self, req: dict) -> dict:
        cmd = str(req.get("cmd", "")).replace(".", "_").replace("-", "_")
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return {"error": f"comando sconosciuto: {req.get('cmd')!r}"}
        with self.lock:
            return fn(req)

    # basic --------------------------------------------------------------------
    def cmd_ping(self, req):
        return {"ok": True, "version": __version__, "dry_run": paths.DRY_RUN}

    # units --------------------------------------------------------------------
    UNIT_ACTIONS = {
        "start": ("start",), "stop": ("stop",), "restart": ("restart",),
        "enable": ("enable", "--now"), "disable": ("disable", "--now"),
    }

    def cmd_unit(self, req):
        unit, action = str(req.get("unit", "")), str(req.get("action", ""))
        if unit not in components.system_units():
            return {"error": f"unità non gestita dall'hub: {unit}"}
        if action not in self.UNIT_ACTIONS:
            return {"error": f"azione non valida: {action}"}
        rc, out = systemctl(*self.UNIT_ACTIONS[action], unit)
        log(f"unit {action} {unit}: rc={rc}")
        return {"ok": rc == 0, "rc": rc, "output": out} if rc == 0 else \
            {"error": f"systemctl {action} {unit} fallito (rc={rc}): {out[-400:]}"}

    # the OMEN group (system half; the session half is the GUI's) -------------
    def cmd_group(self, req):
        action = str(req.get("action", "status"))
        if action == "status":
            return {"ok": True, **group.status("system")}
        try:
            st = group.act("system", action)
        except (ValueError, RuntimeError) as exc:
            return {"error": str(exc)}
        log(f"group {action}: target {st['target_active']}")
        return {"ok": True, **st}

    # components ---------------------------------------------------------------
    def cmd_component(self, req):
        c = components.BY_ID.get(str(req.get("id", "")))
        action = str(req.get("action", ""))
        if c is None:
            return {"error": "componente sconosciuto"}
        if c.hook and action in ("install", "uninstall"):
            return self._hook(c, action)
        argv = c.install if action == "install" else c.uninstall if action == "uninstall" else None
        if action not in ("install", "uninstall"):
            return {"error": f"azione non valida: {action}"}
        if not argv:
            return {"error": f"{c.name}: nessun {'installer' if action == 'install' else 'disinstallatore'} disponibile"}
        script = Path(argv[1]) if len(argv) > 1 else Path(argv[0])
        if not script.exists():
            return {"error": f"{script} non trovato"}
        log(f"component {action} {c.id}: {' '.join(argv)}")
        rc, out = run(list(argv), timeout=900, cwd=c.cwd)
        systemctl("daemon-reload")
        if rc != 0:
            return {"error": f"{c.name}: {action} fallito (rc={rc})\n{out[-1500:]}"}
        return {"ok": True, "output": out[-4000:]}

    def _hook(self, c, action):
        name = c.hook
        target = paths.SLEEP_DIR / name
        parked = paths.DISABLED_HOOKS / name
        if not c.removable_hook:
            return {"error": f"{name} appartiene a un pacchetto: non viene gestito dall'hub"}
        if action == "uninstall":
            if not target.exists():
                return {"error": f"{name} non è installato"}
            paths.DISABLED_HOOKS.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(parked))
            os.chmod(parked, 0o644)      # parked copies are never executable
            log(f"hook {name} parcheggiato in {parked}")
            return {"ok": True, "output": f"{name} spostato in {parked}"}
        if target.exists():
            os.chmod(target, 0o755)
            return {"ok": True, "output": f"{name} già presente: reso eseguibile"}
        src = parked if parked.exists() else paths.PAYLOAD / "hooks" / name
        if not src.exists():
            return {"error": f"nessuna copia di {name} disponibile da reinstallare"}
        paths.SLEEP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{name}.omen-hub.tmp")
        shutil.copy2(src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
        if src == parked:
            parked.unlink()
        log(f"hook {name} installato da {src}")
        return {"ok": True, "output": f"{name} installato da {src}"}

    # profiles -----------------------------------------------------------------
    def _validated_store(self, store):
        curves = list((load_fan_config().get("curves") or {}).keys())
        return profiles.validate_store(store, platform_choices=platform_choices(),
                                       curves=curves, freq_range=freq_range())

    def cmd_profiles_get(self, req):
        return {"ok": True, "store": profiles.load(paths.PROFILES, paths.AUTOCPUFREQ_CONF)}

    def cmd_profiles_save(self, req):
        try:
            store = self._validated_store(req.get("store") or {})
        except ValueError as exc:
            return {"error": str(exc)}
        write_atomic(paths.PROFILES, json.dumps(store, indent=2, ensure_ascii=False) + "\n")
        return self._apply(store)

    def cmd_profiles_apply(self, req):
        try:
            store = self._validated_store(profiles.load(paths.PROFILES, paths.AUTOCPUFREQ_CONF))
        except ValueError as exc:
            return {"error": str(exc)}
        return self._apply(store)

    def _apply(self, store) -> dict:
        ac = store["profiles"][store["assign"]["ac"]]
        bat = store["profiles"][store["assign"]["battery"]]
        try:
            old = paths.AUTOCPUFREQ_CONF.read_text()
        except OSError:
            old = "# auto-cpufreq configuration\n"
        new = cpuconf.render(old, ac, bat)
        changed = new != old
        msgs = []
        if changed:
            write_atomic(paths.AUTOCPUFREQ_CONF, new)
            rc, out = systemctl("restart", "auto-cpufreq.service")
            msgs.append("auto-cpufreq aggiornato e riavviato" if rc == 0 else f"riavvio auto-cpufreq fallito: {out}")
        else:
            msgs.append("auto-cpufreq già allineato")
        fan_msg = self.apply_fan_for_source(store)
        if fan_msg:
            msgs.append(fan_msg)
        msgs += self.enforce_limits(store)
        return {"ok": True, "output": "; ".join(msgs), "changed": changed}

    def profile_in_force(self, store=None) -> tuple[str | None, dict]:
        if store is None:
            try:
                store = json.loads(paths.PROFILES.read_text())
            except (OSError, ValueError):
                return None, {}
        src = "ac" if on_ac() is not False else "battery"
        name = store.get("assign", {}).get(src)
        return name, store.get("profiles", {}).get(name) or {}

    def enforce_limits(self, store=None, periodic=False) -> list[str]:
        _name, prof = self.profile_in_force(store)
        try:
            return limits.enforce(prof, periodic=periodic)
        except Exception as exc:
            traceback.print_exc()
            return [f"limiti non applicati: {exc}"]

    def cmd_limits_info(self, req):
        try:
            return {"ok": True, **limits.info()}
        except Exception as exc:
            return {"error": f"lettura limiti fallita: {exc}"}

    def apply_fan_for_source(self, store=None) -> str | None:
        """Impose the fan setting of the profile in force, or give back the
        user's own setting when that profile leaves the fans alone."""
        if store is None:
            try:
                store = profiles.load(paths.PROFILES)
            except Exception:
                return None
        src = "ac" if on_ac() is not False else "battery"
        name = store.get("assign", {}).get(src)
        prof = store.get("profiles", {}).get(name) or {}
        fan = prof.get("fan")
        cfg = load_fan_config()
        if not cfg:
            return None
        try:
            saved = json.loads(paths.FAN_OVERRIDE.read_text())
        except (OSError, ValueError):
            saved = None
        if fan:
            if saved is None:
                saved = {"mode": cfg.get("mode", "curve"), "active_curve": cfg.get("active_curve"),
                         "manual_pwm": cfg.get("manual_pwm")}
            changes = {"mode": fan["mode"]}
            if fan["mode"] == "curve" and fan.get("curve"):
                changes["active_curve"] = fan["curve"]
            msg = f"ventole: {fan['mode']}" + (f" ({fan['curve']})" if fan.get("curve") else "")
        elif saved is not None:
            changes = {"mode": saved.get("mode") or "curve"}
            if saved.get("active_curve") in (cfg.get("curves") or {}):
                changes["active_curve"] = saved["active_curve"]
            if saved.get("manual_pwm") is not None:
                changes["manual_pwm"] = saved["manual_pwm"]
            msg = f"ventole ripristinate: {changes['mode']}"
        else:
            return None
        try:
            new = fanconf.apply_changes(cfg, changes)
        except ValueError as exc:
            return f"ventole non applicate: {exc}"
        if fan:
            write_atomic(paths.FAN_OVERRIDE, json.dumps({**saved, "profile": name}) + "\n",
                         keep_backup=False)
        else:
            paths.FAN_OVERRIDE.unlink(missing_ok=True)
        if new != cfg:
            write_atomic(paths.FAN_CONF, json.dumps(new, indent=4) + "\n", keep_backup=False)
            return msg
        return None

    # fans -----------------------------------------------------------------------
    def cmd_fan_set(self, req):
        changes = req.get("changes")
        if not isinstance(changes, dict) or not changes:
            return {"error": "nessuna modifica"}
        cfg = load_fan_config()
        if not cfg:
            return {"error": f"{paths.FAN_CONF} assente o illeggibile"}
        try:
            new = fanconf.apply_changes(cfg, changes)
        except ValueError as exc:
            return {"error": str(exc)}
        write_atomic(paths.FAN_CONF, json.dumps(new, indent=4) + "\n")
        if "mode" in changes or "active_curve" in changes:
            # an explicit choice from the user becomes the new base to go back to
            paths.FAN_OVERRIDE.unlink(missing_ok=True)
        log(f"fan config: {', '.join(changes)}")
        return {"ok": True, "config": new}

    def cmd_fan_clean(self, req):
        action = req.get("action")
        if action == "stop":
            paths.FAN_CLEANER_STOP.parent.mkdir(parents=True, exist_ok=True)
            paths.FAN_CLEANER_STOP.touch()
            return {"ok": True}
        if action != "run":
            return {"error": "azione pulizia non valida"}
        rc, _ = systemctl("is-active", "--quiet", "omen-fan-control.service")
        if rc != 0 and not paths.DRY_RUN:
            return {"error": "il servizio ventole non è attivo: la pulizia passa da lì"}
        duration = req.get("duration")
        speed = req.get("speed")
        try:
            if duration is not None:
                duration = fanconf._int(duration, *fanconf.CLEANER_DURATION_RANGE, "durata")
            if speed is not None:
                speed = fanconf._int(speed, *fanconf.CLEANER_SPEED_RANGE, "velocità")
        except ValueError as exc:
            return {"error": str(exc)}
        paths.FAN_CLEANER_REQUEST.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(paths.FAN_CLEANER_REQUEST,
                     json.dumps({"ts": time.time(), "duration": duration, "speed": speed}),
                     keep_backup=False)
        return {"ok": True}

    # misc -----------------------------------------------------------------------
    def cmd_powertop_tune(self, req):
        # restart, not a bare --auto-tune: the unit's ExecStartPost puts the
        # OMEN keyboard back out of USB autosuspend afterwards.
        rc, out = systemctl("restart", "powertop.service", timeout=120)
        return {"ok": True, "output": out} if rc == 0 else {"error": f"powertop: {out}"}

    # tests (Test page) ------------------------------------------------------------
    # Read-only probes that need root, plus the RTC alarm for the standby test.
    RTC_RANGE = (60, 1800)

    def cmd_test_rapl(self, req):
        """Energy counters of the RAPL zones (energy_uj is root-only since
        the PLATYPUS side channel)."""
        zones = {}
        for d in sorted(Path("/sys/class/powercap").glob("intel-rapl:*")):
            name, uj = read_sys(str(d / "name")), read_sys(str(d / "energy_uj"))
            if name and uj and uj.isdigit():
                rng = read_sys(str(d / "max_energy_range_uj")) or "0"
                zones[d.name] = {"name": name, "uj": int(uj), "max": int(rng) if rng.isdigit() else 0}
        return {"ok": True, "t": time.time(), "zones": zones}

    def cmd_test_rtcwake(self, req):
        try:
            secs = int(req.get("seconds"))
        except (TypeError, ValueError):
            return {"error": "secondi non validi"}
        lo, hi = self.RTC_RANGE
        if not lo <= secs <= hi:
            return {"error": f"la sveglia va tra {lo} e {hi} s"}
        if paths.DRY_RUN:
            return {"ok": True, "dry_run": True}
        # -m no: only arms the alarm; the GUI suspends through logind so that
        # the sleep hooks run (rtcwake -m mem would bypass them)
        rc, out = run(["rtcwake", "-m", "no", "-s", str(secs)], timeout=15)
        return {"ok": True} if rc == 0 else {"error": f"rtcwake: {out.strip()}"}

    def cmd_test_gpu_holders(self, req):
        """Processes with the NVIDIA device nodes open: what a normal user
        cannot see for kwin, Xwayland, nvidia-powerd..."""
        gpu = next(iter(Path("/sys/bus/pci/drivers/nvidia").glob("0000:*")), None)
        nodes = {"/dev/nvidiactl", "/dev/nvidia-modeset", "/dev/nvidia-uvm"}
        nodes.update(str(p) for p in Path("/dev").glob("nvidia[0-9]*"))
        if gpu:
            for link in Path("/dev/dri/by-path").glob(f"pci-{gpu.name}-*"):
                nodes.add(str(link.resolve()))
        holders: dict[int, dict] = {}
        for fd in Path("/proc").glob("[0-9]*/fd/*"):
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target in nodes:
                pid = int(fd.parts[2])
                h = holders.setdefault(pid, {"pid": pid, "fds": 0, "nodes": set()})
                h["fds"] += 1
                h["nodes"].add(target)
        out = []
        for pid, h in sorted(holders.items()):
            comm = read_sys(f"/proc/{pid}/comm") or "?"
            out.append({"pid": pid, "comm": comm, "fds": h["fds"], "nodes": sorted(h["nodes"])})
        return {"ok": True, "holders": out}


# -- power source watcher --------------------------------------------------------

def watch_power(hub: Hub, stop: threading.Event, interval: float = 4.0):
    """Fan setting on plug/unplug, and keeps power/ratio limits in place
    (after a resume, or if anything else rewrites them). Only reads unless
    something differs; the GPU is touched only while it is awake."""
    last = on_ac()
    first = True
    while first or not stop.wait(interval):
        first = False
        cur = on_ac()
        with hub.lock:
            try:
                if cur is not None and cur != last:
                    last = cur
                    msg = hub.apply_fan_for_source()
                    if msg:
                        log(f"alimentazione {'rete' if cur else 'batteria'}: {msg}")
                msgs = hub.enforce_limits(periodic=True)
                if msgs:
                    log("limiti: " + "; ".join(msgs))
            except Exception:
                traceback.print_exc()


# -- server ------------------------------------------------------------------------

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock: socket.socket = self.request
        sock.settimeout(10)
        hub: Hub = self.server.hub
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, gid = struct.unpack("3i", creds)
        except OSError:
            return
        if not hub.authorized(uid, gid):
            self._reply({"error": "non autorizzato"})
            return
        data = bytearray()
        try:
            while len(data) < MAX_REQUEST:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
            req = json.loads(bytes(data).split(b"\n", 1)[0] or b"{}")
            if not isinstance(req, dict):
                raise ValueError
        except (ValueError, OSError):
            self._reply({"error": "richiesta non valida"})
            return
        try:
            reply = hub.handle(req)
        except Exception as exc:
            traceback.print_exc()
            reply = {"error": f"errore interno: {exc}"}
        self._reply(reply)

    def _reply(self, obj):
        try:
            self.request.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # install.sh / uninstall.sh: the PartOf drop-ins of the OMEN group.
    if argv[:1] == ["--setup-group"]:
        print("\n".join(group.write_dropins()))
        return 0
    if argv[:1] == ["--remove-group"]:
        print("\n".join(group.remove_dropins()))
        return 0
    sock_path = paths.SOCKET
    hub = Hub()
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    server = Server(sock_path, Handler)
    server.hub = hub
    os.chmod(sock_path, 0o660)
    if os.geteuid() == 0 and hub.allowed_gid is not None:
        os.chown(sock_path, 0, hub.allowed_gid)
    stop = threading.Event()
    threading.Thread(target=watch_power, args=(hub, stop), daemon=True).start()
    log(f"omen-hubd {__version__} in ascolto su {sock_path}"
        + (" (dry run)" if paths.DRY_RUN else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
