"""The guided tests, as generators of steps (no Qt in here).

A test yields steps and gets their results back:

    samples = yield Measure("cpu", "Sotto carico", "...", 45, probe)

The Test page runs the step (instruction with a button, wait until the user
has unplugged something, a countdown that samples, a blocking job in a
thread) and sends the result into the generator. Each test restores what it
changed in a ``finally``, so stopping half-way leaves fans, load and screens
as they were.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import desktop, protocol, sysinfo, testkit
from .testkit import Probes, ProcTracker, Report, fmt, stats


# -- steps ---------------------------------------------------------------------------

@dataclass
class Say:
    """Instruction; the primary button continues (result True), «Salta»
    returns False when skippable."""
    art: str
    title: str
    text: str
    button: str = "Continua"
    skip: str | None = None
    animate: bool = True


@dataclass
class WaitFor:
    """Waits until ``cond()`` is true (checked every second): True; skipped
    or timed out: False."""
    art: str
    title: str
    text: str
    cond: Callable[[], bool]
    timeout: int = 600
    skip: str | None = "Salta"


@dataclass
class Measure:
    """Countdown that calls ``probe()`` every ``every`` seconds and returns
    the list of samples. ``until(sample)`` may end it early."""
    art: str
    title: str
    text: str
    seconds: int
    probe: Callable[[], dict]
    every: float = 2.0
    animate: bool = False
    speed: float = 1.0
    until: Callable[[dict], bool] | None = None
    skip: str | None = None


@dataclass
class Work:
    """Blocking job run in a worker thread; returns its result."""
    art: str
    title: str
    text: str
    fn: Callable[[], object]
    speed: float = 1.0


# -- shared probes -------------------------------------------------------------------

def hub(req: dict, timeout: float = 15) -> dict:
    return protocol.send(req, timeout=timeout)


def hub_ok() -> bool:
    return protocol.available()


def make_probe(pr: Probes, rapl: bool = True, procs: tuple[str, ...] = (),
               gpu_query: bool = False) -> Callable[[], dict]:
    prev = {"t": time.monotonic(), "cpu": testkit.proc_cpu(procs)}

    def probe() -> dict:
        s = pr.sample()
        if rapl:
            s["cpu_w"] = pr.rapl_watts()
        s["gpu_ms"] = pr.gpu_suspended_ms()
        s["screens_off"] = pr.screens_off()
        s["hdmi"] = pr.hdmi_connected()
        if procs:
            now, cpu = time.monotonic(), testkit.proc_cpu(procs)
            dt = max(0.2, now - prev["t"])
            for n in procs:
                if n in cpu and n in prev["cpu"]:
                    s[f"cpu_{n}"] = round(100.0 * (cpu[n] - prev["cpu"][n]) / ProcTracker.HZ / dt, 1)
            prev.update(t=now, cpu=cpu)
        if gpu_query and s["gpu"] == "active":
            g = sysinfo.nvidia_smi_query()
            if g:
                s.update({f"g_{k}": v for k, v in g.items()})
        return s
    return probe


def series(samples: list[dict], key: str) -> list:
    return [s.get(key) for s in samples]


def compact(samples: list[dict], keys: tuple[str, ...]) -> list[list]:
    """Raw samples for the JSON block: t relative to the first one."""
    if not samples:
        return []
    t0 = samples[0]["t"]
    return [[round(s["t"] - t0, 1)] + [s.get(k) for k in keys] for s in samples]


def fan_mode(mode: str):
    hub({"cmd": "fan.set", "changes": {"mode": mode}})


# -- 1. fans ------------------------------------------------------------------------------

def t_fans(rp: Report, pr: Probes):
    rp.section("fans", "Ventole")
    if not hub_ok():
        rp.verdict("skip", "omen-hubd non attivo: il test ventole ha bisogno di lui.")
        return
    cfg = desktop.fan_config()
    old = cfg.get("mode") or "curve"
    rp.line(f"Modalità ventole prima del test: **{old}**"
            + (f" (curva «{cfg.get('active_curve')}», sorgente {cfg.get('temp_source')})" if old == "curve" else ""))
    yield Say("fan", "Test delle ventole",
              "Porto le ventole al massimo per circa 25 secondi e poi le rimetto come prima: "
              "misuro i giri raggiunti, quanto ci mettono a salire e se tornano giù. "
              "Il rumore forte è normale.", "Avvia il test")
    probe = make_probe(pr, rapl=False)
    base = yield Measure("fan", "Ventole a riposo", "Misuro i giri attuali.", 6, probe, every=1, animate=True, speed=0.3)
    try:
        yield Work("fan", "Ventole al massimo", "Invio il comando al controllo ventole…", lambda: fan_mode("max"), speed=2)
        t_cmd = time.time()
        up = yield Measure("fan", "Ventole al massimo", "Ascolta: dovrebbero salire in pochi secondi.",
                           25, probe, every=1, animate=True, speed=3)
    finally:
        try:
            fan_mode(old)
        except protocol.HubError as exc:
            rp.verdict("fail", f"non sono riuscito a rimettere la modalità «{old}»: {exc}")
    down = yield Measure("fan", "Ritorno alla normalità", f"Modalità «{old}» ripristinata: le ventole devono scendere.",
                         25, probe, every=1, animate=True, speed=0.8)

    rows, ok_all = [], True
    for key, name in (("fan1", "CPU"), ("fan2", "GPU")):
        b = stats(series(base, key))
        u = series(up, key)
        top = max([v for v in u if v is not None] or [0])
        t90 = next((s["t"] - t_cmd for s in up if (s.get(key) or 0) >= 0.9 * top and top > 0), None)
        end = stats(series(down[-5:], key))["avg"]
        rows.append([name, fmt(b["avg"], "{:.0f}"), top, fmt(t90, "{:.0f}", "s"), fmt(end, "{:.0f}")])
        if top < 4000:
            ok_all = False
            rp.verdict("fail", f"ventola {name}: massimo {top} RPM (atteso ≥ 4000): ventola o driver da controllare.")
        elif t90 is not None and t90 > 15:
            rp.verdict("warn", f"ventola {name}: lenta a salire ({t90:.0f} s per arrivare al 90 %).")
        if end is not None and top and end > 0.6 * top:
            ok_all = False
            rp.verdict("warn", f"ventola {name}: dopo il ripristino è ancora a {end:.0f} RPM.")
    rp.table(["Ventola", "A riposo (RPM)", "Massimo (RPM)", "Tempo al 90 %", "Dopo 25 s di ripristino (RPM)"], rows)
    if ok_all:
        rp.verdict("ok", "entrambe le ventole arrivano al massimo e tornano alla curva.")
    rp.put("before_mode", old)
    rp.put("samples [t, fan1, fan2]", {"base": compact(base, ("fan1", "fan2")),
                                       "max": compact(up, ("fan1", "fan2")),
                                       "restore": compact(down, ("fan1", "fan2"))})


# -- 2. CPU -------------------------------------------------------------------------------

def t_cpu(rp: Report, pr: Probes):
    rp.section("cpu", "CPU sotto carico")
    yield Say("cpu", "Test della CPU",
              "Carico tutti i core per 45 secondi (stress-ng) e poi lascio raffreddare 30 secondi: "
              "frequenze P/E, temperature, potenza del package e risposta delle ventole. "
              "Chiudi i programmi pesanti prima di partire.", "Avvia il test")
    probe = make_probe(pr)
    probe()      # primes the RAPL counter
    idle = yield Measure("cpu", "Riposo", "Valori di partenza.", 10, probe, every=2, animate=True, speed=0.2)
    load = testkit.Load()
    try:
        load.cpu(45)
        busy = yield Measure("cpu", "Sotto carico", "Tutti i core al 100 %: le ventole devono salire.",
                             45, probe, every=2, animate=True, speed=1.0)
    finally:
        load.stop()
    cool = yield Measure("cpu", "Raffreddamento", "Carico tolto: la temperatura deve scendere.",
                         30, probe, every=2, animate=True, speed=0.3)

    s = pr.sampler
    hw_p = (s.hw_max_p or 0) / 1000
    i, b, c = ({k: stats(series(x, k)) for k in ("pkg", "ctrl", "fp", "fe", "cpu_w", "fan1", "fan2", "load")}
               for x in (idle, busy, cool))
    rp.table(["", "Riposo", "Sotto carico", "Raffreddamento"], [
        ["Temperatura controllo (°C, media/max)", fmt(i["ctrl"]["avg"]), f"{fmt(b['ctrl']['avg'])} / {fmt(b['ctrl']['max'])}", fmt(c["ctrl"]["avg"])],
        ["Package (°C, max)", fmt(i["pkg"]["max"]), fmt(b["pkg"]["max"]), fmt(c["pkg"]["max"])],
        ["P-core (MHz, media)", fmt(i["fp"]["avg"], "{:.0f}"), fmt(b["fp"]["avg"], "{:.0f}"), fmt(c["fp"]["avg"], "{:.0f}")],
        ["E-core (MHz, media)", fmt(i["fe"]["avg"], "{:.0f}"), fmt(b["fe"]["avg"], "{:.0f}"), fmt(c["fe"]["avg"], "{:.0f}")],
        ["CPU package (W, RAPL)", fmt(i["cpu_w"]["avg"]), fmt(b["cpu_w"]["avg"]), fmt(c["cpu_w"]["avg"])],
        ["Ventola CPU (RPM, max)", fmt(i["fan1"]["max"], "{:.0f}"), fmt(b["fan1"]["max"], "{:.0f}"), fmt(c["fan1"]["max"], "{:.0f}")],
        ["Carico (%)", fmt(i["load"]["avg"], "{:.0f}"), fmt(b["load"]["avg"], "{:.0f}"), fmt(c["load"]["avg"], "{:.0f}")],
    ])
    if (b["load"]["avg"] or 0) < 80:
        rp.verdict("warn", f"il carico è arrivato solo al {fmt(b['load']['avg'], '{:.0f}')} %: risultato poco indicativo.")
    throttled = bool(hw_p and b["fp"]["avg"] and b["fp"]["avg"] < 0.5 * hw_p)
    if b["ctrl"]["max"] and b["ctrl"]["avg"] and b["ctrl"]["avg"] >= 97:
        rp.verdict("warn", f"temperatura di controllo in media a {b['ctrl']['avg']:.0f} °C per tutto il carico: "
                           "raffreddamento al limite (polvere? pulizia ventole?).")
    elif b["ctrl"]["max"]:
        rp.verdict("ok", f"sotto carico pieno {fmt(b['cpu_w']['avg'], '{:.0f}')} W, P-core a "
                         f"{fmt(b['fp']['avg'], '{:.0f}')} MHz, temperatura di controllo media "
                         f"{b['ctrl']['avg']:.0f} °C (picchi a {b['ctrl']['max']:.0f} °C: normali, TjMax 105 °C).")
    if b["fan1"]["max"] is not None and i["fan1"]["avg"] is not None and b["fan1"]["max"] < (i["fan1"]["avg"] or 0) + 800:
        rp.verdict("warn", "le ventole non sono salite in modo evidente sotto carico: controllare la curva.")
    if i["cpu_w"]["avg"] is not None:
        rp.line(f"Il «riposo» di questo test dura 10 s: è solo il punto di partenza ({i['cpu_w']['avg']:.1f} W). "
                "Il consumo a riposo vero lo misura il test «Consumi a riposo».")
    if throttled:
        rp.verdict("warn", f"P-core in media a {b['fp']['avg']:.0f} MHz sotto carico su {hw_p:.0f} massimi: "
                           "limite di potenza o termico.")
    if c["ctrl"]["avg"] and b["ctrl"]["max"] and c["ctrl"]["avg"] > b["ctrl"]["max"] - 5:
        rp.verdict("warn", "dopo 30 s senza carico la temperatura non è scesa.")
    rp.put("samples [t, pkg, ctrl, fp, fe, cpu_w, fan1, fan2, load]", {
        k: compact(v, ("pkg", "ctrl", "fp", "fe", "cpu_w", "fan1", "fan2", "load"))
        for k, v in (("idle", idle), ("load", busy), ("cool", cool))})


# -- 3. GPU -------------------------------------------------------------------------------

def _holders_text(h):
    if h is None:
        return "(elenco non disponibile: serve omen-hubd)"
    if not h:
        return "nessun processo"
    return ", ".join(f"{x['comm']} [{x['pid']}] {x['fds']} fd" for x in h)


def t_gpu(rp: Report, pr: Probes):
    rp.section("gpu", "GPU NVIDIA")
    if not pr.gpu:
        rp.verdict("skip", "nessuna GPU NVIDIA trovata.")
        return
    yield Say("gpu", "Test della GPU",
              "Verifico che la RTX si spenga da sola quando non serve (D3cold, ~0 W), quanto ci mette "
              "a rispegnersi dopo un risveglio e, se vuoi, come va sotto carico.", "Avvia il test")
    if pr.hdmi_connected():
        ok = yield WaitFor("unplug_hdmi", "Stacca l'HDMI",
                           "La porta HDMI è collegata alla NVIDIA: con il monitor attaccato la GPU deve "
                           "restare accesa. Staccalo per questo test (o salta per misurare comunque).",
                           lambda: not pr.hdmi_connected(), timeout=300)
        rp.line("HDMI " + ("staccato per il test." if ok else "lasciato collegato."))
    probe = make_probe(pr, rapl=False)
    idle = yield Measure("gpu", "La GPU si spegne?", "Aspetto fino a 30 secondi che vada in D3cold.",
                         30, probe, every=1, speed=0.0, until=lambda s: s.get("gpu") == "suspended")
    off = bool(idle) and idle[-1].get("gpu") == "suspended"
    holders = None
    if off:
        rp.verdict("ok", f"a riposo la GPU va in D3cold (suspended) dopo {idle[-1]['t'] - idle[0]['t']:.0f} s.")
    else:
        holders = yield Work("gpu", "Chi la tiene accesa?", "Cerco i processi che usano la NVIDIA…",
                             Probes.gpu_holders, speed=0.3)
        rp.verdict("fail" if not pr.hdmi_connected() else "info",
                   "la GPU resta accesa a riposo. Processi con la NVIDIA aperta: " + _holders_text(holders)
                   + ". Se nessuno la usa davvero: auto-cpufreq con enforce_platform_profile, una regola "
                     "udev con power/control=on, o un contesto Vulkan/compute.")
    rp.line(f"Stato a riposo, un campione al secondo: {' → '.join(dict.fromkeys(s.get('gpu') or '?' for s in idle))}"
            + (" (HDMI collegato)." if pr.hdmi_connected() else "."))
    rp.line("")
    rp.put("idle_state", [s.get("gpu") for s in idle])
    rp.put("holders", holders)

    # wake-up and back
    if off:
        def wake():
            subprocess.run(["nvidia-smi", "-L"], capture_output=True, timeout=20)
            return time.time()
        t_wake = yield Work("gpu", "Sveglio la GPU", "Una chiamata a nvidia-smi la porta in D0…", wake, speed=1.0)
        back = yield Measure("gpu", "Torna a spegnersi?", "Dopo un risveglio deve rispegnersi in pochi secondi.",
                             40, probe, every=1, speed=0.0,
                             until=lambda s: s.get("gpu") == "suspended" and s["t"] > t_wake + 1)
        if back and back[-1].get("gpu") == "suspended":
            dt = back[-1]["t"] - t_wake
            rp.verdict("ok" if dt <= 30 else "warn", f"dopo un risveglio torna in D3cold in {dt:.0f} s"
                       + (" (il driver aspetta ~20 s prima di rispegnerla)." if dt <= 30 else "."))
            rp.put("resuspend_s", round(dt, 1))
        else:
            rp.verdict("fail", "dopo un risveglio non torna a spegnersi entro 40 s.")

    # optional load
    if testkit.GPU_BURN.is_file():
        go = yield Say("gpu", "Carico sulla GPU (facoltativo)",
                       "30 secondi di gpu_burn: temperatura, potenza, clock e ventole sotto carico pieno.",
                       "Avvia il carico", skip="Salta")
        if go:
            gp = make_probe(pr, rapl=True, gpu_query=True)
            load = testkit.Load()
            try:
                load.gpu(30)
                busy = yield Measure("gpu", "GPU sotto carico", "gpu_burn al lavoro: le ventole salgono.",
                                     32, gp, every=2, animate=True, speed=1.5)
            finally:
                load.stop()
            st = {k: stats(series(busy, k)) for k in ("g_temp", "g_power", "g_clock", "g_util", "fan2", "cpu_w")}
            rp.table(["Sotto carico", "media", "max"], [
                ["GPU °C", st["g_temp"]["avg"], st["g_temp"]["max"]],
                ["GPU W", st["g_power"]["avg"], st["g_power"]["max"]],
                ["Clock MHz", st["g_clock"]["avg"], st["g_clock"]["max"]],
                ["Utilizzo %", st["g_util"]["avg"], st["g_util"]["max"]],
                ["Ventola GPU RPM", st["fan2"]["avg"], st["fan2"]["max"]],
            ])
            if (st["g_util"]["avg"] or 0) < 50:
                rp.verdict("warn", "gpu_burn non ha caricato la GPU (utilizzo basso): controlla CUDA.")
            elif (st["g_temp"]["max"] or 0) >= 87:
                rp.verdict("warn", f"GPU a {st['g_temp']['max']:.0f} °C sotto carico.")
            else:
                rp.verdict("ok", f"sotto carico {fmt(st['g_power']['avg'], '{:.0f}')} W, "
                                 f"{fmt(st['g_temp']['max'], '{:.0f}')} °C max, {fmt(st['g_clock']['avg'], '{:.0f}')} MHz.")
            rp.put("load [t, g_temp, g_power, g_clock, g_util, fan2]",
                   compact(busy, ("g_temp", "g_power", "g_clock", "g_util", "fan2")))
            after = yield Measure("gpu", "Si rispegne dopo il carico?", "Aspetto che torni in D3cold.",
                                  45, probe, every=1, speed=0.0, until=lambda s: s.get("gpu") == "suspended")
            if after and after[-1].get("gpu") == "suspended":
                rp.verdict("ok", f"dopo il carico torna in D3cold in {after[-1]['t'] - after[0]['t']:.0f} s.")
            else:
                rp.verdict("warn", "dopo il carico è ancora accesa dopo 45 s.")


# -- 4. idle power, guided -------------------------------------------------------------------

def _idle_block(rp, pr, title, seconds):
    probe = make_probe(pr)
    probe()
    procs = ProcTracker()
    samples = yield Measure("hands_off", title,
                            "Non toccare mouse e tastiera: la pagina resta ferma apposta, "
                            "anche le animazioni consumano.", seconds, probe, every=5, animate=False)
    total, top = procs.top(10)
    return samples, total, top


def t_idle(rp: Report, pr: Probes):
    rp.section("idle", "Consumi a riposo")
    yield Say("hands_off", "Test dei consumi a riposo",
              "Ti guido passo passo: misuro il consumo reale a batteria senza e con il monitor HDMI, "
              "poi la CPU sotto rete. In tutto circa 6 minuti, quasi tutti senza toccare niente.",
              "Iniziamo")
    on_bat = yield WaitFor("unplug_ac", "Stacca il caricatore",
                           "Il consumo reale si legge solo a batteria. Stacca il cavo di alimentazione.",
                           lambda: pr.ac_online() is False, timeout=300, skip="Salta (resto sotto rete)")
    if pr.hdmi_connected():
        yield WaitFor("unplug_hdmi", "Stacca l'HDMI", "Prima misura senza monitor esterno.",
                      lambda: not pr.hdmi_connected(), timeout=300)
    yield Say("hands_off", "Due minuti senza toccare niente",
              "Appena premi «Avvia», allontana le mani: misuro per 2 minuti consumo, CPU, GPU e processi.",
              "Avvia la misura", animate=False)
    samples, total, top = yield from _idle_block(rp, pr, "Misura a riposo, senza HDMI", 120)
    bw = stats(series(samples, "bat_w"))
    cw = stats(series(samples, "cpu_w"))
    gpu_off = sum(1 for s in samples if s.get("gpu") == "suspended")
    rp.line("### Batteria, senza monitor esterno" if on_bat else "### Sotto rete, senza monitor esterno")
    rp.table(["Misura", "Valore"], [
        ["Consumo totale dalla batteria (W, media / min / max)",
         f"{fmt(bw['avg'])} / {fmt(bw['min'])} / {fmt(bw['max'])}" if bw["n"] else "– (sotto rete)"],
        ["CPU package RAPL (W, media)", fmt(cw["avg"])],
        ["GPU in D3cold", f"{gpu_off}/{len(samples)} campioni"],
        ["Temperatura controllo (°C, media)", fmt(stats(series(samples, 'ctrl'))['avg'])],
        ["Ventole (RPM, max)", f"{stats(series(samples, 'fan1'))['max']} / {stats(series(samples, 'fan2'))['max']}"],
        ["CPU usata da tutti i processi (% di un core)", f"{total:.1f}"],
    ])
    rp.line("Processi più attivi: " + ", ".join(f"{r['comm']} {r['cpu']} %" for r in top))
    rp.line("")
    if bw["n"]:
        kind = "ok" if bw["avg"] <= 11 else "warn" if bw["avg"] <= 15 else "fail"
        rp.verdict(kind, f"a riposo a batteria senza HDMI: {bw['avg']:.1f} W"
                   + {"ok": " (riferimento 8,6 W del 18/09).", "warn": ": un po' alto, guarda GPU e processi.",
                      "fail": ": alto. Di solito è la GPU accesa o un processo che non dorme."}[kind])
    if samples and gpu_off < len(samples) * 0.8:
        rp.verdict("fail", "la GPU NVIDIA è rimasta accesa durante la misura a riposo (~6-10 W sprecati).")
    if top and top[0]["cpu"] > 15:
        rp.verdict("warn", f"«{top[0]['comm']}» ha usato il {top[0]['cpu']} % di un core a riposo.")
    rp.put("battery_noHDMI [t, bat_w, cpu_w, gpu, ctrl, fan1, fan2]",
           compact(samples, ("bat_w", "cpu_w", "gpu", "ctrl", "fan1", "fan2")))
    rp.put("top_processes_noHDMI", top)

    # with the HDMI monitor
    go = yield Say("plug_hdmi", "Anche con il monitor HDMI?",
                   "Facoltativo: misuro quanto costa il monitor esterno (la GPU deve accendersi) e se, "
                   "staccandolo, la GPU si rispegne da sola.", "Sì, misura", skip="Salta")
    if go:
        plugged = yield WaitFor("plug_hdmi", "Collega l'HDMI", "Collega il monitor esterno e aspetta che si accenda.",
                                lambda: pr.hdmi_connected(), timeout=300)
        if plugged:
            yield Say("hands_off", "Un minuto senza toccare niente", "Misuro con il monitor collegato.",
                      "Avvia la misura", animate=False)
            hs, htotal, htop = yield from _idle_block(rp, pr, "Misura con HDMI collegato", 60)
            hw = stats(series(hs, "bat_w"))
            rp.line("### Con il monitor HDMI collegato")
            rp.table(["Misura", "Valore"], [
                ["Consumo totale dalla batteria (W, media)", fmt(hw["avg"]) if hw["n"] else "– (sotto rete)"],
                ["CPU package RAPL (W, media)", fmt(stats(series(hs, 'cpu_w'))['avg'])],
                ["GPU attiva", f"{sum(1 for s in hs if s.get('gpu') == 'active')}/{len(hs)} campioni"],
            ])
            if hw["n"] and bw["n"]:
                rp.verdict("info", f"il monitor HDMI costa circa {hw['avg'] - bw['avg']:.1f} W in più (GPU accesa).")
            rp.put("battery_HDMI [t, bat_w, cpu_w, gpu]", compact(hs, ("bat_w", "cpu_w", "gpu")))
            unplugged = yield WaitFor("unplug_hdmi", "Ora stacca l'HDMI",
                                      "Stacca il cavo: misuro in quanto tempo la GPU si rispegne.",
                                      lambda: not pr.hdmi_connected(), timeout=300)
            if unplugged:
                t0 = time.time()
                probe = make_probe(pr, rapl=False)
                back = yield Measure("gpu", "La GPU si rispegne?", "Aspetto fino a 60 secondi.", 60, probe,
                                     every=1, speed=0.0, until=lambda s: s.get("gpu") == "suspended")
                if back and back[-1].get("gpu") == "suspended":
                    rp.verdict("ok", f"staccato l'HDMI, la GPU torna in D3cold in {back[-1]['t'] - t0:.0f} s.")
                else:
                    holders = Probes.gpu_holders()
                    rp.verdict("fail", "staccato l'HDMI, la GPU resta accesa. Processi: " + _holders_text(holders))

    # on mains
    plugged_ac = yield WaitFor("plug_ac", "Ricollega il caricatore",
                               "Ultima parte: CPU a riposo sotto rete (il profilo del caricatore è diverso).",
                               lambda: pr.ac_online() is True, timeout=300)
    if plugged_ac:
        yield Say("hands_off", "Un minuto senza toccare niente", "Misuro la CPU a riposo sotto rete.",
                  "Avvia la misura", animate=False)
        ms, mtotal, mtop = yield from _idle_block(rp, pr, "Misura sotto rete", 60)
        mw = stats(series(ms, "cpu_w"))
        rp.line("### Sotto rete")
        rp.table(["Misura", "Valore"], [
            ["CPU package RAPL (W, media)", fmt(mw["avg"])],
            ["Temperatura controllo (°C, media)", fmt(stats(series(ms, 'ctrl'))['avg'])],
            ["Ventole (RPM, max)", f"{stats(series(ms, 'fan1'))['max']} / {stats(series(ms, 'fan2'))['max']}"],
            ["Frequenza P-core (MHz, media)", fmt(stats(series(ms, 'fp'))['avg'], '{:.0f}')],
        ])
        if mw["avg"] is not None:
            kind = "ok" if mw["avg"] <= 6 else "warn" if mw["avg"] <= 9 else "fail"
            rp.verdict(kind, f"CPU a riposo sotto rete: {mw['avg']:.1f} W"
                       + ("" if kind == "ok" else " (il 12/10 era 8,8 W per frequenza minima 1,4 GHz + profilo performance)."))
        rp.put("mains [t, cpu_w, ctrl, fan1, fan2, fp]", compact(ms, ("cpu_w", "ctrl", "fan1", "fan2", "fp")))
        rp.put("top_processes_mains", mtop)


# -- 5. screen off ---------------------------------------------------------------------------

def _screen_off():
    subprocess.run(["loginctl", "lock-session"], capture_output=True, timeout=10)
    time.sleep(3)
    subprocess.run(["kscreen-doctor", "--dpms", "off"], capture_output=True, timeout=10)
    return time.time()


def t_screen(rp: Report, pr: Probes):
    rp.section("screen", "Schermo spento")
    yield Say("screen_off", "Test a schermo spento",
              "Blocco la sessione e spengo gli schermi per 90 secondi, come quando ti allontani. "
              "Controllo che plasmashell e kwin non si mettano a ridisegnare senza sosta (bug del 12/10) "
              "e che le ventole restino tranquille. Non toccare niente finché non te lo dico.",
              "Spegni gli schermi")
    t0 = yield Work("screen_off", "Spengo gli schermi", "Blocco la sessione…", _screen_off)
    probe = make_probe(pr, procs=("plasmashell", "kwin_wayland"))
    probe()
    samples = yield Measure("screen_off", "Schermi spenti", "Misuro per 90 secondi.", 90, probe, every=3)
    yield WaitFor("wake", "Riaccendi lo schermo", "Muovi il mouse e sblocca la sessione.",
                  lambda: not pr.screens_off(), timeout=900, skip="Continua")
    off = [s for s in samples if s.get("screens_off")]
    ps = stats(series(off, "cpu_plasmashell"))
    kw = stats(series(off, "cpu_kwin_wayland"))
    ctrl = stats(series(off, "ctrl"))
    f1 = stats(series(off, "fan1"))
    guard = testkit.run(["journalctl", "--user", "--no-pager", "-o", "cat", "-u",
                         "plasmashell-dpms-guard.service", "--since", f"@{int(t0) - 5}"])
    rp.table(["Misura (a schermi spenti)", "media", "max"], [
        ["plasmashell CPU %", ps["avg"], ps["max"]],
        ["kwin CPU %", kw["avg"], kw["max"]],
        ["Temperatura controllo °C", ctrl["avg"], ctrl["max"]],
        ["Ventola CPU RPM", f1["avg"], f1["max"]],
    ])
    rp.line(f"Campioni con schermi davvero spenti: {len(off)}/{len(samples)}. HDMI collegato: "
            + ("sì" if any(s.get("hdmi") for s in samples) else "no") + ".")
    if guard.strip():
        rp.line("Interventi del guardiano: " + "; ".join(guard.strip().splitlines()[-4:]))
    if not off:
        rp.verdict("warn", "gli schermi non risultano spenti: kscreen-doctor non ha funzionato?")
    elif (ps["max"] or 0) > 40 or (kw["max"] or 0) > 40:
        rp.verdict("fail", f"a schermi spenti plasmashell/kwin al {ps['max']}/{kw['max']} %: il loop di ridisegno c'è ancora"
                   + (" (il guardiano è intervenuto)." if "SIGSTOP" in guard else "."))
    else:
        rp.verdict("ok", f"a schermi spenti plasmashell {fmt(ps['avg'])} %, kwin {fmt(kw['avg'])} %: nessun loop.")
    if (f1["max"] or 0) > 2500:
        rp.verdict("warn", f"ventole fino a {f1['max']} RPM con gli schermi spenti.")
    rp.put("samples [t, screens_off, cpu_plasmashell, cpu_kwin_wayland, ctrl, fan1]",
           compact(samples, ("screens_off", "cpu_plasmashell", "cpu_kwin_wayland", "ctrl", "fan1")))


# -- 6. standby -------------------------------------------------------------------------------

SLEEP_SECONDS = 180


def _sleep_cycle(pr: Probes, seconds: int) -> dict:
    """Arms the RTC, suspends through logind (so the sleep hooks run) and
    samples the fans every 0.25 s across the sleep."""
    hub({"cmd": "test.rtcwake", "seconds": seconds})
    before = pr.suspend_stats()
    bat0 = Probes.battery_pct()
    hp = pr.sampler.hp
    fast: list[tuple[float, int | None, int | None]] = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            fast.append((time.time(),
                         testkit._int(hp / "fan1_input") if hp else None,
                         testkit._int(hp / "fan2_input") if hp else None))
            stop.wait(0.25)
    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(1.5)
    t_sleep = time.time()
    subprocess.run(["systemctl", "suspend"], capture_output=True, timeout=30)
    deadline = time.monotonic() + seconds + 900
    while pr.suspend_stats()["success"] == before["success"] and time.monotonic() < deadline:
        if pr.suspend_stats()["fail"] != before["fail"]:
            break
        time.sleep(0.5)
    time.sleep(10)
    stop.set()
    th.join(2)
    after = pr.suspend_stats()
    # the sleep shows up as a jump in wall-clock time between two samples
    gap = max(range(1, len(fast)), key=lambda k: fast[k][0] - fast[k - 1][0], default=None)
    return {"t_sleep": t_sleep, "before": before, "after": after, "bat0": bat0, "bat1": Probes.battery_pct(),
            "fast": fast, "gap": gap, "slept_s": (fast[gap][0] - fast[gap - 1][0]) if gap else None}


def t_sleep(rp: Report, pr: Probes):
    rp.section("sleep", "Standby (sospensione)")
    if not hub_ok():
        rp.verdict("skip", "omen-hubd non attivo: serve per la sveglia automatica.")
        return
    yield Say("sleep", "Test dello standby",
              f"Il PC va in sospensione per {SLEEP_SECONDS // 60} minuti e si risveglia da solo. "
              "Lascia il coperchio aperto e non toccare niente: al risveglio troverai la schermata di blocco. "
              "Controllo le ventole durante il sonno, quanto dorme davvero e gli errori.",
              "Sospendi adesso")
    res = yield Work("sleep", "In sospensione…", f"Risveglio automatico tra {SLEEP_SECONDS // 60} minuti.",
                     lambda: _sleep_cycle(pr, SLEEP_SECONDS))
    yield WaitFor("wake", "Bentornato", "Sblocca la sessione per vedere i risultati.",
                  lambda: not pr.screens_off(), timeout=900, skip="Continua")
    b, a = res["before"], res["after"]
    if a["success"] == b["success"]:
        rp.verdict("fail", "la sospensione non è avvenuta o non è terminata (contatore success fermo).")
    hw = (a["total_hw_sleep"] - b["total_hw_sleep"]) / 1e6
    slept = res["slept_s"] or 0
    pct = 100 * hw / slept if slept else None
    fast, gap = res["fast"], res["gap"]
    pre = fast[max(0, gap - 4):gap] if gap else []
    post = fast[gap:gap + 12] if gap else []
    post_max = max([max(x[1] or 0, x[2] or 0) for x in post[:4]] or [0])
    rp.table(["Misura", "Valore"], [
        ["Durata del sonno (s)", f"{slept:.0f}"],
        ["Sonno profondo hardware (s, %)", f"{hw:.0f} ({fmt(pct, '{:.0f}')} %)"],
        ["Ventole subito prima (RPM)", ", ".join(f"{x[1]}/{x[2]}" for x in pre)],
        ["Ventole al risveglio, primi 3 s (RPM)", ", ".join(f"{x[1]}/{x[2]}" for x in post)],
        ["Batteria (%)", f"{res['bat0']} → {res['bat1']}"],
    ])
    if pct is not None:
        rp.verdict("ok" if pct >= 85 else "warn" if pct >= 50 else "fail",
                   f"sonno profondo (S0ix) per il {pct:.0f} % del tempo.")
    if post:
        rp.verdict("ok" if post_max == 0 else "warn" if post_max < 1500 else "fail",
                   "ventole ferme durante il sonno." if post_max == 0 else
                   f"al risveglio le ventole erano già a {post_max} RPM: giravano durante il sonno "
                   "(flag PECF all'EC? GPU accesa?).")
    log = testkit.journal_since(res["t_sleep"] - 5,
                                r"ec-standby-flag|PM: suspend|Freezing of tasks failed|PM: .*error|"
                                r"hp-wmi|failed to suspend|resume.*fail")
    if log:
        rp.line("Dal journal:")
        rp.line("```")
        for l in log:
            rp.line(l)
        rp.line("```")
    if not any("ec-standby-flag" in l and "pre" in l for l in log):
        rp.verdict("warn", "nel journal non c'è la scrittura del flag PECF (hook ec-standby-flag).")
    if any("Freezing of tasks failed" in l or "error" in l.lower() for l in log):
        rp.verdict("warn", "errori di sospensione nel journal (vedi sopra).")
    rp.put("fans_around_sleep [t, fan1, fan2]",
           [[round(x[0] - res["t_sleep"], 2), x[1], x[2]] for x in pre + post])
    rp.put("suspend_stats", {"before": b, "after": a})


# -- catalog -------------------------------------------------------------------------------

@dataclass
class TestDef:
    key: str
    title: str
    art: str
    minutes: str
    description: str
    fn: Callable
    needs_hub: bool = False


CATALOG: tuple[TestDef, ...] = (
    TestDef("idle", "Consumi a riposo", "unplug_ac", "≈ 6 min",
            "Guidato: stacca caricatore e HDMI, mani lontane. Watt reali, CPU, GPU e processi; "
            "poi con monitor esterno e sotto rete.", t_idle),
    TestDef("gpu", "GPU NVIDIA", "gpu", "1–3 min",
            "Si spegne da sola (D3cold)? Chi la tiene accesa? Si rispegne dopo un risveglio? "
            "Carico facoltativo con gpu_burn.", t_gpu),
    TestDef("fans", "Ventole", "fan", "≈ 1 min",
            "Al massimo e ritorno: giri raggiunti, tempo di salita, ritorno alla curva.", t_fans, True),
    TestDef("cpu", "CPU sotto carico", "cpu", "≈ 1,5 min",
            "45 s di stress su tutti i core: frequenze P/E, temperature, potenza e ventole.", t_cpu),
    TestDef("screen", "Schermo spento", "screen_off", "≈ 2 min",
            "Blocco + schermi spenti: plasmashell/kwin non devono ridisegnare a vuoto.", t_screen),
    TestDef("sleep", "Standby", "sleep", "≈ 4 min",
            "Sospensione vera con risveglio automatico: ventole nel sonno, S0ix, errori.", t_sleep, True),
)
BY_KEY = {t.key: t for t in CATALOG}
