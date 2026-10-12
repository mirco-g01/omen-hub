"""The OMEN group: every long-running OMEN service as one systemd unit pair.

Two targets, because systemd keeps the system and the session apart:
``omen-hub.target`` (system) and ``omen-hub-user.target`` (session). Each
member gets a drop-in with ``PartOf=`` its target, so stopping or restarting
the target stops or restarts the whole group, from the hub or by hand::

    systemctl restart omen-hub.target
    systemctl --user restart omen-hub-user.target

The targets deliberately carry no ``Wants=``. Membership must not undo a
choice made per service: a member you disabled stays off when the group is
started, and "start" means "start the *enabled* members" -- which is why it
is an action here rather than a plain ``systemctl start`` of the target.

Left out on purpose: omen-hubd (stopping the group must not take away the
helper that starts it again), the sleep hooks and their one-shot units (they
run on events, not continuously) and the desktop extras (controller, terminal
panels, baloo), which are not about the laptop's hardware.
"""

from __future__ import annotations

import subprocess

from . import components, paths

SYSTEM_TARGET = "omen-hub.target"
USER_TARGET = "omen-hub-user.target"
DROPIN = "50-omen-hub.conf"
GROUPS = ("Ventole e termica", "Energia e CPU", "Illuminazione e tastiera")

SYSTEMD_SYSTEM = paths.P("/etc/systemd/system")
SYSTEMD_USER = paths.P("/etc/systemd/user")


def members(scope: str) -> list[str]:
    """Long-running units of the hardware components, in registry order."""
    return [u.name for c in components.COMPONENTS if c.group in GROUPS
            for u in c.units if u.scope == scope and not u.oneshot]


def target(scope: str) -> str:
    return SYSTEM_TARGET if scope == "system" else USER_TARGET


def dropin_text(scope: str) -> str:
    return ("# Written by OMEN Hub (omen_hub/group.py): stopping or restarting\n"
            f"# {target(scope)} stops or restarts this unit too.\n"
            f"[Unit]\nPartOf={target(scope)}\n")


def write_dropins() -> list[str]:
    """Root side, from install.sh: one PartOf drop-in per member."""
    done = []
    for scope, base in (("system", SYSTEMD_SYSTEM), ("user", SYSTEMD_USER)):
        for unit in members(scope):
            d = base / f"{unit}.d"
            d.mkdir(parents=True, exist_ok=True)
            (d / DROPIN).write_text(dropin_text(scope))
            done.append(str(d / DROPIN))
    return done


def remove_dropins() -> list[str]:
    gone = []
    for base in (SYSTEMD_SYSTEM, SYSTEMD_USER):
        for f in base.glob(f"*.d/{DROPIN}"):
            f.unlink()
            gone.append(str(f))
            try:
                f.parent.rmdir()          # only if nothing else lives there
            except OSError:
                pass
    return gone


def _systemctl(scope: str, *args: str, timeout: int = 120) -> tuple[int, str]:
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + list(args)
    if paths.DRY_RUN and args and args[0] in ("start", "stop", "restart"):
        return 0, "dry run: " + " ".join(cmd)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def status(scope: str) -> dict:
    """Target state plus each member's active/enabled state."""
    units = members(scope)
    rc, out = _systemctl(scope, "show", "-p", "Id,LoadState,ActiveState,UnitFileState",
                         target(scope), *units, timeout=15)
    blocks, cur = [], {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        blocks.append(cur)
    by_id = {b.get("Id"): b for b in blocks}
    t = by_id.get(target(scope), {})
    rows = [{"name": u, "load": by_id.get(u, {}).get("LoadState", "not-found"),
             "active": by_id.get(u, {}).get("ActiveState", "inactive"),
             "enabled": by_id.get(u, {}).get("UnitFileState", "")} for u in units]
    return {"scope": scope, "target": target(scope),
            "target_load": t.get("LoadState", "not-found"),
            "target_active": t.get("ActiveState", "inactive"), "units": rows}


def act(scope: str, action: str) -> dict:
    """start / stop / restart the group of one scope.

    start   the target, then every member that is installed and enabled
    stop    the target: PartOf takes the members down with it
    restart the target (members follow); a target that was stopped is
            started instead, since restarting nothing would leave it empty
    """
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"azione non valida: {action}")
    st = status(scope)
    if st["target_load"] != "loaded":
        raise RuntimeError(f"{target(scope)} non installato: rilancia install.sh")
    if action == "restart" and st["target_active"] != "active":
        action = "start"
    if action == "start":
        todo = [u["name"] for u in st["units"]
                if u["load"] == "loaded" and u["enabled"] in ("enabled", "enabled-runtime", "static")]
        rc, out = _systemctl(scope, "start", target(scope), *todo)
    else:
        rc, out = _systemctl(scope, action, target(scope))
    if rc != 0:
        raise RuntimeError(f"systemctl {action} {target(scope)} fallito (rc={rc}): {out[-400:]}")
    return status(scope)
