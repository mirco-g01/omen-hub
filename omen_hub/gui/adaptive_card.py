"""Luci e schermo → Luminosità adattiva.

The switch of omen-hub-luce.service, the two curves (room light → screen %,
→ keyboard %), the advanced settings and the calibration. Settings are saved
to adaptive.CONFIG and the service is told with SIGHUP; what it is doing is
read back from adaptive.STATUS every 2 s while the page is open.

Calibration runs here, with the camera: the service sees the node opened by
someone else and steps aside for as long as it lasts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QDoubleSpinBox, QFormLayout, QGraphicsOpacityEffect, QGridLayout,
                             QHBoxLayout, QPushButton, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from .. import adaptive as A
from .. import camlight
from .. import trayapplet
from . import widgets as W

CAL_SECONDS = 10
CAL_LEAD = 3
TORCH_MIN = A.TORCH_MIN
CAL_TEXT = {
    "dark": ("Spegni le luci della stanza e lascia lo schermo com'è: la condizione più buia in cui "
             "usi il PC. Non coprire la fotocamera."),
    "bright": ("Accendi la torcia del telefono e appoggiala sulla fotocamera, al centro sopra lo "
               "schermo. Tienila ferma finché non finisce."),
}


def systemctl(*args: str) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 1, "systemctl non disponibile"
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def num(v: float, digits: int = 3) -> str:
    return f"{v:.{digits}g}".replace(".", ",")


def alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


class LightCurveEditor(W.CurveEditor):
    T_MIN, T_MAX = 0.0, 100.0
    X_TICKS = range(0, 101, 25)
    X_TICK_FMT = "{:.0f}%"
    POINT_FMT = "luce {t:.0f} % → {v:.0f} %"
    MARKER_FMT = "ora: luce {t:.0f} % → {v:.0f} %"
    X_TITLE = "Luce della stanza (0 = buio, 100 = torcia)"

    def __init__(self, y_title: str, parent=None):
        super().__init__(parent)
        self.y_title = y_title
        self.setMinimumHeight(240)
        self.setToolTip("Trascina i punti; doppio clic ne aggiunge uno, clic destro lo toglie.\n"
                        "La luce della stanza è in scala logaritmica, come la vede l'occhio.")
        self._fx = QGraphicsOpacityEffect(self)
        self._fx.setOpacity(1.0)
        self.setGraphicsEffect(self._fx)

    def changeEvent(self, e):
        super().changeEvent(e)
        fx = getattr(self, "_fx", None)          # also called from the base constructor
        if fx is not None:
            fx.setOpacity(1.0 if self.isEnabled() else 0.4)


class OutputColumn(QWidget):
    """One output (screen or keyboard): on/off, when, one curve per power
    source (the editor shows one at a time), what it does now."""
    changed = pyqtSignal()
    TABS = [("battery", "Curva a batteria"), ("ac", "Curva a rete")]

    def __init__(self, title: str, y_title: str):
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self.switch = W.Switch(title)
        self.when = W.Segmented([("battery", "Solo a batteria"), ("always", "Anche a rete")])
        top = QHBoxLayout()
        top.addWidget(self.switch)
        top.addStretch(1)
        top.addWidget(self.when)
        col.addLayout(top)
        # which of the two curves the editor shows ("Anche a rete" only)
        self.which = W.Segmented(self.TABS)
        self.which_box = QWidget()
        wl = QHBoxLayout(self.which_box)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addStretch(1)
        wl.addWidget(self.which)
        col.addWidget(self.which_box)
        self.curve = LightCurveEditor(y_title)
        col.addWidget(self.curve)
        self.note = W.label("", "muted", wrap=True)
        col.addWidget(self.note)
        self.curves: dict[str, list] = {p: [] for p in A.PROFILES}
        self.shown = "battery"
        self.picked = False           # the user chose a tab: stop following the power source
        self._loading = False
        self.switch.toggled.connect(self._toggled)
        self.when.changed.connect(self._when_changed)
        self.which.changed.connect(self._pick)
        self.curve.committed.connect(self._curve_edited)

    # editing ------------------------------------------------------------------------------------
    def _sync(self):
        if self.curve.points:
            self.curves[self.shown] = [[round(t, 1), round(v, 1)] for t, v in self.curve.points]

    def _show_curve(self, prof: str):
        self._sync()
        self.shown = prof
        self.which.set(prof)
        self.curve.set_points(self.curves[prof])

    def _pick(self, prof: str):
        self.picked = True
        self._show_curve(prof)

    def _toggled(self, on: bool):
        self._enable(on)
        self._edited()

    def _enable(self, on: bool):
        for w in (self.when, self.which, self.curve):
            w.setEnabled(on)

    def _when_changed(self, key: str):
        self.which_box.setVisible(key == "always")
        if key == "battery" and self.shown != "battery":
            self._show_curve("battery")
        self._edited()

    def _curve_edited(self):
        self._sync()
        self._edited()

    def _edited(self):
        if not self._loading:
            self.changed.emit()

    def load(self, o: dict):
        self._loading = True
        self.switch.setChecked(o["enabled"])
        self.when.set(o["when"])
        self.curves = {p: [list(pt) for pt in o["curves"][p]] for p in A.PROFILES}
        if o["when"] == "battery":
            self.shown = "battery"
        self.which.set(self.shown)
        self.curve.set_points(self.curves[self.shown])
        self.which_box.setVisible(o["when"] == "always")
        self._enable(o["enabled"])
        self._loading = False

    def values(self) -> dict:
        self._sync()
        return {"enabled": self.switch.isChecked(), "when": self.when.value() or "always",
                "curves": {p: [list(pt) for pt in self.curves[p]] for p in A.PROFILES}}

    # what the service does ------------------------------------------------------------------
    def show_status(self, st: dict | None, x: float | None, profile: str | None,
                    on_battery: bool | None, running: bool, resume_delta: int = 30):
        o = self.values()
        # the editor follows the power source, unless you picked a curve yourself
        if o["when"] == "battery":
            tab = "battery"
        elif not self.picked and profile in A.PROFILES:
            tab = profile
        else:
            tab = self.shown
        if tab != self.shown and self.curve.drag is None:
            self._show_curve(tab)
        mark = None
        if not o["enabled"]:
            text = "Spento: resta dove lo metti tu."
        elif not running:
            text = ""
        elif o["when"] == "battery" and on_battery is False:
            text = "In pausa: attivo solo a batteria, ora sei a rete."
        elif st and st.get("paused"):
            p = st["paused"]
            since = time.strftime("%H:%M", time.localtime(p["since"]))
            until = time.strftime("%H:%M", time.localtime(p["until"])) if p.get("until") else "?"
            how = "l'hai cambiato a mano" if p.get("why") == "a mano" else "spento dal pannello"
            text = f"Sospeso: {how} alle {since}. Riprende alle {until}"
            text += (f" o se la luce cambia di {resume_delta} punti." if resume_delta < A.RESUME_RANGE[1] else ".")
        elif st and st.get("skipped"):
            text = f"Lasciato stare: {st['skipped']}."
        elif st and st.get("target") is not None and x is not None:
            in_use = "curva a batteria" if profile == "battery" else "curva a rete"
            level = st.get("level") if st.get("level") is not None else st["target"]
            text = f"Ora {level:.0f} %"
            if st.get("anchor") is not None:
                text += f", impostato con luce {st['anchor']:.0f} %"
            if o["when"] == "always":
                text += f" ({in_use})"
            if abs(level - st["target"]) >= 1 and st.get("anchor") is not None:
                text += (f". La curva darebbe {st['target']:.0f} %, ma la luce si è spostata di "
                         f"{abs(x - st['anchor']):.0f} punti, meno della variazione minima")
            if profile == self.shown:
                mark = (x, level)
            else:
                text += f". Stai guardando l'altra curva: ora vale la {in_use}."
        else:
            text = "In attesa della prima misura."
        self.note.setText(text)
        self.curve.set_marker(*(mark or (None, None)))


class _Progress(QObject):
    value = pyqtSignal(float)


class AdaptiveCard(W.Card):
    def __init__(self, ctx):
        super().__init__("Luminosità adattiva",
                         "Schermo e tastiera seguono la luce della stanza, misurata con uno scatto della "
                         "webcam. Mai mentre un'altra app usa la fotocamera (videochiamate, howdy): "
                         "il servizio aspetta che la lasci.")
        self.ctx = ctx
        self.pill = W.Pill()
        self.header.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)
        self.master = W.Switch("Regola la luminosità con la luce ambiente")
        self.master.toggled.connect(self._toggle_service)
        self.state_lbl = W.label("", "muted", wrap=True)
        self.add(self.master)
        self.add(self.state_lbl)

        cols = QHBoxLayout()
        cols.setSpacing(32)
        self.cols = {"screen": OutputColumn("☀  Schermo", "Luminosità schermo"),
                     "keyboard": OutputColumn("⌨  Tastiera", "Luminosità tastiera")}
        for c in self.cols.values():
            c.changed.connect(self._save_soon)
            cols.addWidget(c, 1)
        self.add(cols)

        self.btn_adv = QToolButton(text="Avanzate", checkable=True)
        self.btn_adv.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_adv.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_adv.setAutoRaise(True)
        self.btn_adv.toggled.connect(self._toggle_adv)
        self.btn_forget = QPushButton("Riprendi la regolazione")
        self.btn_forget.setToolTip("Se cambi la luminosità a mano (tasti, cursori, il pannello\n"
                                   "Luminosità e colore) la regolazione automatica di quell'uscita si\n"
                                   "sospende. Questo pulsante la riprende subito per entrambe.")
        self.btn_forget.clicked.connect(self._resume_all)
        self.btn_now = QPushButton("Misura ora")
        self.btn_now.clicked.connect(lambda: self._signal("USR1", "Misura richiesta"))
        self.add(W.hrow(self.btn_adv, None, self.btn_forget, self.btn_now))

        self.adv = self._build_advanced()
        self.adv.setVisible(False)
        self.add(self.adv)

        self.cfg = A.load()
        self.status: dict | None = None
        self.running = False
        self._service_loading = False
        self._loading = False
        self._save_timer = QTimer(self, singleShot=True, interval=350, timeout=self._save)
        self._poll = QTimer(self, interval=2000, timeout=self._refresh_status)
        self._cal: dict | None = None
        self._lead = QTimer(self, interval=1000, timeout=self._lead_tick)
        self._progress = _Progress()
        self._progress.value.connect(self._cal_value)
        self._load_cfg()

    # -- advanced ----------------------------------------------------------------------------
    def _build_advanced(self) -> QWidget:
        adv = QWidget()
        v = QVBoxLayout(adv)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(12)
        v.addWidget(W.label("IMPOSTAZIONI", "statLabel"))
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(*A.INTERVAL_RANGE)
        self.sp_interval.setSingleStep(5)
        self.sp_interval.setSuffix(" s")
        self.sp_exposure = QSpinBox()
        self.sp_exposure.setRange(*A.EXPOSURE_RANGE)
        self.sp_exposure.setSingleStep(25)
        self.sp_exposure.setSuffix(" ms")
        self.sp_transition = QDoubleSpinBox()
        self.sp_transition.setRange(*A.TRANSITION_RANGE)
        self.sp_transition.setDecimals(1)
        self.sp_transition.setSingleStep(0.5)
        self.sp_transition.setSuffix(" s")
        self.sp_delta = QSpinBox()
        self.sp_delta.setRange(*A.DELTA_RANGE)
        self.sp_delta.setSuffix(" %")
        self.sp_pause = QDoubleSpinBox()
        self.sp_pause.setRange(*A.PAUSE_RANGE)
        self.sp_pause.setDecimals(2)
        self.sp_pause.setSingleStep(0.5)
        self.sp_pause.setSuffix(" h")
        self.sp_resume = QSpinBox()
        self.sp_resume.setRange(*A.RESUME_RANGE)
        self.sp_resume.setSingleStep(5)
        self.sp_resume.setSuffix(" punti")
        for sp in (self.sp_interval, self.sp_exposure, self.sp_transition, self.sp_delta,
                   self.sp_pause, self.sp_resume):
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(self._save_soon)
        def row(spin, hint):
            r = QHBoxLayout()
            r.setSpacing(12)
            r.addWidget(spin, 0, Qt.AlignmentFlag.AlignTop)
            r.addWidget(W.label(hint, "faint", wrap=True), 1)
            return r
        form.addRow("Intervallo tra scatti", row(
            self.sp_interval, "ogni scatto accende il LED della fotocamera per circa 0,3 s"))
        form.addRow("Esposizione massima", row(
            self.sp_exposure, "solo per lo scatto lungo al buio (LED fino a ~1 s): quando la stanza si "
                              "scurisce, poi ogni 10 minuti"))
        form.addRow("Transizione", row(
            self.sp_transition, "quanto dura il passaggio da un livello all'altro (0 = di colpo)"))
        form.addRow("Variazione minima della luce", row(
            self.sp_delta, "il livello cambia solo se la luce della stanza si sposta almeno di tanto "
                           "dalla misura che ha impostato il livello attuale"))
        form.addRow("Pausa dopo un cambio a mano", row(
            self.sp_pause, "se cambi tu la luminosità (tasti, cursori, pannello), la regolazione "
                           "automatica di quell'uscita si ferma per questo tempo…"))
        form.addRow("…o finché la luce cambia di", row(
            self.sp_resume, "punti della scala rispetto a quando l'hai cambiata (100 = solo il timer)"))
        v.addLayout(form)

        v.addWidget(W.label("CALIBRAZIONE", "statLabel"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.lb_dark, self.lb_bright = W.label(""), W.label("")
        self.btn_dark = QPushButton(f"Misura il buio ({CAL_SECONDS} s)")
        self.btn_bright = QPushButton(f"Misura con la torcia ({CAL_SECONDS} s)")
        self.btn_dark.clicked.connect(lambda: self._start_cal("dark"))
        self.btn_bright.clicked.connect(lambda: self._start_cal("bright"))
        grid.addWidget(W.label("Buio (0 %)"), 0, 0)
        grid.addWidget(self.lb_dark, 0, 1)
        grid.addWidget(self.btn_dark, 0, 2)
        grid.addWidget(W.label("Luce massima (100 %)"), 1, 0)
        grid.addWidget(self.lb_bright, 1, 1)
        grid.addWidget(self.btn_bright, 1, 2)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        self.cal_msg = W.label("", wrap=True)
        self.cal_msg.setVisible(False)
        v.addWidget(self.cal_msg)
        self.btn_cal_reset = QPushButton("Calibrazione predefinita")
        self.btn_cal_reset.clicked.connect(self._reset_cal)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(W.label(
            "Il buio è la stanza più buia in cui usi il PC; la luce massima, la torcia del telefono "
            "sulla fotocamera. Dal buio registrato si prende il minimo, dalla torcia il massimo. "
            "Durante la misura il servizio si mette da parte.", "faint", wrap=True), 1)
        row.addWidget(self.btn_cal_reset, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(row)
        self.last_lbl = W.label("", "faint", wrap=True)
        v.addWidget(self.last_lbl)

        v.addWidget(W.label("PANNELLO LUMINOSITÀ E COLORE", "statLabel"))
        self.tray_lbl = W.label("Controllo del pannello…", wrap=True)
        self.btn_tray = QPushButton("Aggiungi l'interruttore")
        self.btn_tray.setEnabled(False)
        self.btn_tray.clicked.connect(self._toggle_tray)
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self.tray_lbl, 1)
        row.addWidget(self.btn_tray, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(row)
        v.addWidget(W.label(
            "Aggiunge l'interruttore «Luminosità adattiva» al pannello del vassoio: spegnerlo vale come "
            "cambiare la luminosità a mano. Il pannello viene sostituito da una copia identica con "
            "l'interruttore in più; plasmashell si riavvia (barre e desktop spariscono per un attimo).",
            "faint", wrap=True))
        return adv

    def _toggle_adv(self, on: bool):
        self.btn_adv.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.adv.setVisible(on)
        if on:
            self._load_tray()

    # -- the tray switch ----------------------------------------------------------------------
    def _load_tray(self):
        def show(st):
            on = st["nel_vassoio"]
            text = ("Interruttore presente nel pannello Luminosità e colore." if on else
                    "Il pannello Luminosità e colore è quello originale, senza interruttore.")
            if on and not st["allineato"]:
                text += (f" Attenzione: la copia è fatta su {st['base']}, ora c'è powerdevil "
                         f"{st['powerdevil']}: se il pannello dà errori, togli l'interruttore.")
            self.tray_lbl.setText(text)
            self.btn_tray.setText("Togli l'interruttore" if on else "Aggiungi l'interruttore")
            self.btn_tray.setEnabled(True)
            self._tray_on = on
        W.run_async(trayapplet.status, show, lambda m: self.tray_lbl.setText(m))

    def _toggle_tray(self):
        self.btn_tray.setEnabled(False)
        self.tray_lbl.setText("Riavvio di plasmashell…")
        fn = trayapplet.remove if getattr(self, "_tray_on", False) else trayapplet.install
        W.run_async(fn, lambda msg: (self.ctx.toast(msg, "ok"), self._load_tray()),
                    lambda m: (self.ctx.toast(m, "err"), self._load_tray()))

    # -- page hooks ----------------------------------------------------------------------------
    def activated(self):
        if not self._save_timer.isActive() and self._cal is None:
            self.cfg = A.load()
            self._load_cfg()
        self._load_service()
        self._refresh_status()
        self._poll.start()

    def deactivated(self):
        self._poll.stop()
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()

    # -- settings ------------------------------------------------------------------------------
    def _load_cfg(self):
        self._loading = True
        for out, col in self.cols.items():
            col.load(self.cfg[out])
        self.sp_interval.setValue(self.cfg["interval_s"])
        self.sp_exposure.setValue(self.cfg["max_exposure_ms"])
        self.sp_transition.setValue(self.cfg["transition_s"])
        self.sp_delta.setValue(self.cfg["min_delta"])
        self.sp_pause.setValue(self.cfg["pause_hours"])
        self.sp_resume.setValue(self.cfg["resume_delta"])
        self._loading = False
        self._show_cal()

    def _save_soon(self, *_):
        if not self._loading:
            self._save_timer.start()

    def _save(self):
        cfg = dict(self.cfg)
        for out, col in self.cols.items():
            cfg[out] = col.values()
        cfg["interval_s"] = self.sp_interval.value()
        cfg["max_exposure_ms"] = self.sp_exposure.value()
        cfg["transition_s"] = round(self.sp_transition.value(), 1)
        cfg["min_delta"] = self.sp_delta.value()
        cfg["pause_hours"] = round(self.sp_pause.value(), 2)
        cfg["resume_delta"] = self.sp_resume.value()
        try:
            self.cfg = A.save(cfg)
        except OSError as exc:
            self.ctx.toast(f"Impostazioni non salvate: {exc}", "err")
            return
        self._signal("HUP")
        self._refresh_status()

    def _signal(self, sig: str, ok_msg: str | None = None):
        if not self.running:
            if ok_msg:
                self.ctx.toast("Il servizio è spento", "err")
            return

        def act():
            code, out = systemctl("kill", "-s", sig, "--kill-whom=main", A.UNIT)
            if code:
                raise RuntimeError(out or f"systemctl kill {sig}")
        W.run_async(act, (lambda _: self.ctx.toast(ok_msg, "ok")) if ok_msg else None,
                    lambda m: self.ctx.toast(m, "err"))
        if sig in ("USR1", "USR2"):
            QTimer.singleShot(2500, self._refresh_status)

    def _resume_all(self):
        try:
            A.write_request("riprendi", list(A.OUTPUTS))
        except OSError as exc:
            self.ctx.toast(str(exc), "err")
            return
        self._signal("USR2", "Regolazione ripresa")

    # -- the service ---------------------------------------------------------------------------
    def _load_service(self):
        def read():
            _, out = systemctl("show", "-p", "LoadState", "-p", "UnitFileState", "-p", "ActiveState", A.UNIT)
            return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

        def show(props):
            load, enabled, active = (props.get("LoadState", ""), props.get("UnitFileState", ""),
                                     props.get("ActiveState", ""))
            self._service_loading = True
            if load in ("", "not-found"):
                self.master.setChecked(False)
                self.master.setEnabled(False)
                self.pill.set("missing")
                self.running = False
                self.state_lbl.setText("Servizio non installato: esegui una volta sudo ~/omen-hub/install.sh")
            else:
                self.master.setEnabled(True)
                self.master.setChecked(enabled == "enabled" or active in ("active", "activating"))
                self.running = active in ("active", "activating", "reloading")
                self.pill.set("ok" if self.running else "failed" if active == "failed" else "off")
            self._service_loading = False
            self._refresh_status()
        W.run_async(read, show, lambda m: self.ctx.toast(m, "err"))

    def _toggle_service(self, on: bool):
        if self._service_loading:
            return
        self.master.setEnabled(False)

        def act():
            code, out = systemctl("enable" if on else "disable", "--now", A.UNIT)
            if code:
                raise RuntimeError(out.splitlines()[-1] if out else f"systemctl {code}")

        def done(_):
            self.ctx.toast("Luminosità adattiva " + ("attivata" if on else "disattivata"), "ok")
            self._load_service()

        def failed(msg):
            self.ctx.toast(msg, "err")
            self._load_service()
        W.run_async(act, done, failed)

    def _refresh_status(self):
        st = A.read_status()
        if st and not alive(st.get("pid")):
            st = None
        self.status = st
        reading = (st or {}).get("reading") or {}
        x = reading.get("x")
        live = bool(st) and self.running
        for out, col in self.cols.items():
            prof = (st or {}).get("profile") or (
                A.profile_of((st or {}).get("on_battery")) if st and st.get("on_battery") is not None else None)
            col.show_status((st or {}).get(out), x, prof, (st or {}).get("on_battery"), live,
                            self.cfg["resume_delta"])
        if self.master.isEnabled() is False and not self.running:
            return                                   # "not installed" text stays
        if not self.running:
            self.state_lbl.setText("Spento: schermo e tastiera restano dove li metti tu "
                                   "(e ai livelli per alimentazione qui sotto).")
        elif not st:
            self.state_lbl.setText("Avvio del servizio…")
        else:
            state, reason = st.get("state"), st.get("reason") or ""
            when = time.strftime("%H:%M:%S", time.localtime(reading["at"])) if reading.get("at") else None
            if state == "ok" and when:
                text = f"Ultima misura alle {when}: luce della stanza {x:.0f} %."
            elif state == "attesa":
                text = f"In attesa: {reason}."
            elif state == "pausa":
                text = f"In pausa: {reason}."
            elif state == "errore":
                text = f"Errore: {reason}."
            else:
                text = "Avvio…"
            if st.get("next_at") and state in ("ok", "attesa"):
                left = max(0, int(st["next_at"] - time.time()))
                text += f" Prossimo scatto tra {left} s."
            self.state_lbl.setText(text)
        self.btn_forget.setEnabled(self.running and any(((st or {}).get(o) or {}).get("paused")
                                                        for o in A.OUTPUTS))
        self.btn_now.setEnabled(self.running)
        if reading:
            shots = reading.get("shots", 1)
            text = (f"Ultima lettura: stima {num(reading['lum'])} (1 ≈ stanza in penombra), posa "
                    f"{num(reading['exposure_ms'])} ms, {shots} scatt{'o' if shots == 1 else 'i'}, "
                    f"fotocamera accesa {num(reading['on_s'], 2)} s.")
            if reading.get("source") == "buio confermato" and reading.get("long_at"):
                text += (" Ancora buio: vale lo scatto lungo delle "
                         + time.strftime("%H:%M", time.localtime(reading["long_at"])) + ".")
            self.last_lbl.setText(text)
        else:
            self.last_lbl.setText("")

    # -- calibration ---------------------------------------------------------------------------
    def _show_cal(self):
        cal, d = self.cfg["calibration"], A.DEFAULTS["calibration"]
        for end, lb in (("dark", self.lb_dark), ("bright", self.lb_bright)):
            at = cal.get(f"{end}_at")
            if at:
                lb.setText(f"{num(cal[end])} · misurato il {time.strftime('%d/%m %H:%M', time.localtime(at))}")
            else:
                lb.setText(f"{num(cal[end])} (predefinito)" if cal[end] == d[end] else num(cal[end]))
        self.btn_cal_reset.setEnabled(cal["dark"] != d["dark"] or cal["bright"] != d["bright"])

    def _start_cal(self, kind: str):
        if self._cal is not None:
            return
        dev = ((self.status or {}).get("device")) or camlight.find_device()
        if not dev:
            self.ctx.toast("Fotocamera non trovata", "err")
            return
        self._cal = {"kind": kind, "dev": dev, "lead": CAL_LEAD, "values": [], "stop": False,
                     "t0": None}
        for b in (self.btn_dark, self.btn_bright, self.btn_cal_reset):
            b.setEnabled(False)
        self.cal_msg.setVisible(True)
        self._lead_tick()
        self._lead.start()

    def _lead_tick(self):
        c = self._cal
        if c is None:
            self._lead.stop()
            return
        if c["lead"] > 0:
            self.cal_msg.setText(f"<b>{CAL_TEXT[c['kind']]}</b><br>Si parte tra {c['lead']} s…")
            c["lead"] -= 1
            return
        self._lead.stop()
        c["t0"] = time.monotonic()
        self.cal_msg.setText(f"<b>{CAL_TEXT[c['kind']]}</b><br>Registrazione…")
        max_ms = self.cfg["max_exposure_ms"]
        W.run_async(lambda: camlight.sample(c["dev"], CAL_SECONDS, max_ms,
                                            on_value=lambda v, _f: self._progress.value.emit(v),
                                            stop=lambda: c["stop"]),
                    self._cal_done, self._cal_failed)

    def _cal_value(self, v: float):
        c = self._cal
        if c is None or c["t0"] is None:
            return
        c["values"].append(v)
        pick = max if c["kind"] == "bright" else min
        left = max(0, CAL_SECONDS - (time.monotonic() - c["t0"]))
        self.cal_msg.setText(f"<b>{CAL_TEXT[c['kind']]}</b><br>Registrazione… {left:.0f} s · ora {num(v)} · "
                             f"{'massimo' if c['kind'] == 'bright' else 'minimo'} {num(pick(c['values']))}")

    def _cal_end(self):
        self._cal = None
        for b in (self.btn_dark, self.btn_bright):
            b.setEnabled(True)
        self._show_cal()

    def _cal_failed(self, msg: str):
        self.cal_msg.setText(f"Misura non riuscita: {msg}")
        self._cal_end()

    def _cal_done(self, values: list[float]):
        kind = self._cal["kind"] if self._cal else "dark"
        if not values:
            self._cal_failed("nessun fotogramma utile")
            return
        v = max(values) if kind == "bright" else min(values)
        if kind == "bright" and v < TORCH_MIN:
            self.cal_msg.setText(
                f"Misurato al massimo {num(v)}: la torcia non è arrivata alla fotocamera (appoggiata "
                f"sull'obiettivo dà ben più di {TORCH_MIN:.0f}). Calibrazione non cambiata: riprova con la "
                "torcia accesa e appoggiata sul forellino della fotocamera, sopra lo schermo.")
            self._cal_end()
            return
        cal = dict(self.cfg["calibration"])
        cal[kind], cal[f"{kind}_at"], cal["zone"] = v, time.time(), A.CAL_ZONE
        if cal["bright"] <= cal["dark"] * 1.5:
            self.cal_msg.setText(
                f"Misurato {num(v)}, troppo vicino all'altro estremo ({num(cal['dark' if kind == 'bright' else 'bright'])}): "
                + ("avvicina di più la torcia e ripeti." if kind == "bright" else "spegni più luci e ripeti."))
            self._cal_end()
            return
        self.cfg = A.save(dict(self.cfg, calibration=cal))
        self.cal_msg.setText(("Luce massima registrata" if kind == "bright" else "Buio registrato") + f": {num(v)} "
                             f"({len(values)} fotogrammi). Il servizio la usa da subito.")
        self._cal_end()
        self._signal("HUP")
        QTimer.singleShot(1500, self._refresh_status)

    def _reset_cal(self):
        self.cfg = A.save(dict(self.cfg, calibration=dict(A.DEFAULTS["calibration"])))
        self._show_cal()
        self.cal_msg.setVisible(False)
        self._signal("HUP")
        self.ctx.toast("Calibrazione predefinita ripristinata", "ok")
