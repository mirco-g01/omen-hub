"""Ventole: mode, curve library and editor, temperature filter, fan cleaning.

Everything is written to /etc/omen-fan-control/config.json through omen-hubd;
the fan daemon picks it up on its own (it watches the file's mtime)."""

from __future__ import annotations

import datetime as dt

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog,
                             QProgressBar, QPushButton, QSlider, QSpinBox, QVBoxLayout)

from ... import components, desktop, fanconf
from .. import widgets as W
from . import Page

INTERVALS = (("Ogni giorno", 24), ("Ogni 3 giorni", 72), ("Ogni settimana", 168),
             ("Ogni 2 settimane", 336), ("Ogni mese", 720))


def fmt_ts(ts):
    if not ts:
        return "mai"
    d = dt.datetime.fromtimestamp(ts)
    today = dt.date.today()
    if d.date() == today:
        return f"oggi {d:%H:%M}"
    if d.date() == today + dt.timedelta(days=1):
        return f"domani {d:%H:%M}"
    return f"{d:%d/%m %H:%M}"


class FansPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Ventole",
                         "Curve, modalità e pulizia. Il servizio omen-fan-control rilegge la "
                         "configurazione da solo, senza riavvii.")
        self.needs_hub_banner()
        self.cfg: dict = {}
        self.edit_dirty = False
        self.editing: str | None = None

        # live + mode ---------------------------------------------------------------
        top = W.Card("Modalità")
        row = QHBoxLayout()
        row.setSpacing(28)
        self.t_f1 = W.StatTile("Ventola CPU", "RPM", 20)
        self.t_f2 = W.StatTile("Ventola GPU", "RPM", 20)
        self.t_ct = W.StatTile("Temperatura di controllo", "°C", 20)
        self.t_out = W.StatTile("Uscita curva", "%", 20)
        for t in (self.t_f1, self.t_f2, self.t_ct, self.t_out):
            row.addWidget(t)
        row.addStretch(1)
        self.svc_pill = W.Pill()
        row.addWidget(self.svc_pill, 0, Qt.AlignmentFlag.AlignTop)
        top.add(row)
        self.seg_mode = W.Segmented([("curve", "Curva"), ("auto", "Auto (BIOS)"),
                                     ("max", "Massima"), ("manual", "Manuale")])
        self.seg_mode.changed.connect(self._mode)
        self.sl_manual = QSlider(Qt.Orientation.Horizontal)
        self.sl_manual.setRange(0, 100)
        self.sl_manual.setFixedWidth(240)
        self.lb_manual = W.label("", "muted")
        self.sl_manual.valueChanged.connect(lambda v: self.lb_manual.setText(f"{v} %"))
        self.sl_manual.sliderReleased.connect(self._manual)
        top.add(W.hrow(self.seg_mode, W.label("   Velocità manuale"), self.sl_manual, self.lb_manual, None))
        self.mode_hint = W.label("", "faint", wrap=True)
        top.add(self.mode_hint)
        self.add(top)

        # curves ---------------------------------------------------------------------------
        cc = W.Card("Curve", "Trascina i punti · doppio clic aggiunge · clic destro rimuove. "
                             "Il punto arancione è dove si trovano le ventole adesso.")
        bar = QHBoxLayout()
        self.cb_curve = QComboBox()
        self.cb_curve.setMinimumWidth(200)
        self.cb_curve.activated.connect(self._curve_selected)
        self.btn_use = QPushButton("Usa questa curva", objectName="primary")
        self.btn_use.clicked.connect(self._use_curve)
        self.btn_cnew = QPushButton("Nuova")
        self.btn_cren = QPushButton("Rinomina")
        self.btn_cdel = QPushButton("Elimina", objectName="danger")
        self.btn_cnew.clicked.connect(self._curve_new)
        self.btn_cren.clicked.connect(self._curve_rename)
        self.btn_cdel.clicked.connect(self._curve_delete)
        self.active_lbl = W.label("", "muted")
        for w in (self.cb_curve, self.active_lbl):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.btn_cnew, self.btn_cren, self.btn_cdel, self.btn_use):
            bar.addWidget(w)
        cc.add(bar)
        self.editor = W.CurveEditor()
        self.editor.changed.connect(self._curve_touched)
        cc.add(self.editor)
        self.seg_interp = W.Segmented([("smooth", "Interpolata"), ("discrete", "A gradini")])
        self.seg_interp.changed.connect(lambda v: self._set({"curve_interpolation": v}))
        self.btn_csave = QPushButton("Salva punti", objectName="primary")
        self.btn_crevert = QPushButton("Annulla", objectName="ghost")
        self.btn_csave.clicked.connect(self._curve_save)
        self.btn_crevert.clicked.connect(lambda: self._load_curve(self.editing))
        cc.add(W.hrow(self.seg_interp, None, self.btn_crevert, self.btn_csave))
        self.add(cc)

        # temperature filter ---------------------------------------------------------------
        tf = W.Card("Temperatura usata dalla curva",
                    "Package = il core più caldo istante per istante (un P-core a 5 GHz tocca "
                    "100 °C in meno di un secondo). Hybrid = media dei core smussata, più il "
                    "package solo se resta alto per tutta la finestra.")
        self.seg_src = W.Segmented([("hybrid", "Hybrid (consigliata)"), ("core_mean", "Media core"),
                                    ("package", "Package")])
        self.seg_src.changed.connect(lambda v: self._set({"temp_source": v}))
        self.sp_spike, self.sp_ma = QSpinBox(), QSpinBox()
        self.sp_spike.setRange(1, 120)
        self.sp_ma.setRange(1, 120)
        self.sp_spike.setSuffix(" campioni")
        self.sp_ma.setSuffix(" campioni")
        self.btn_filter = QPushButton("Applica")
        self.btn_filter.clicked.connect(lambda: self._set({"spike_window": self.sp_spike.value(),
                                                           "ma_window": self.sp_ma.value()}))
        tf.add(self.seg_src)
        tf.add(W.hrow(W.label("Filtro picchi"), self.sp_spike, W.label("   Media mobile"), self.sp_ma,
                      W.label("(1 campione = 2 s)", "faint"), None, self.btn_filter))
        self.add(tf)

        # cleaning --------------------------------------------------------------------------------
        cl = W.Card("Pulizia ventole",
                    "Le ventole girano al contrario per soffiare via la polvere, poi tornano al "
                    "firmware. Si interrompe da sola sopra 75 °C (media core) e non parte a batteria.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        self.t_last = W.StatTile("Ultima pulizia", "", 13)
        self.t_next = W.StatTile("Prossima automatica", "", 13)
        self.t_state = W.StatTile("Stato", "", 13)
        for i, t in enumerate((self.t_last, self.t_next, self.t_state)):
            grid.addWidget(t, 0, i)
        cl.add(grid)
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(6)
        self.prog.hide()
        cl.add(self.prog)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        self.tg_auto = W.Toggle()
        self.cb_int = QComboBox()
        for lab, h in INTERVALS:
            self.cb_int.addItem(lab, h)
        self.sp_from, self.sp_to = QSpinBox(), QSpinBox()
        for sp in (self.sp_from, self.sp_to):
            sp.setRange(0, 24)
            sp.setSuffix(":00")
        self.sp_dur = QSpinBox()
        self.sp_dur.setRange(*fanconf.CLEANER_DURATION_RANGE)
        self.sp_dur.setSuffix(" s")
        self.sp_speed = QSpinBox()
        self.sp_speed.setRange(fanconf.CLEANER_SPEED_RANGE[0] * 100, fanconf.CLEANER_SPEED_RANGE[1] * 100)
        self.sp_speed.setSingleStep(100)
        self.sp_speed.setSuffix(" RPM")
        for sp in (self.sp_dur, self.sp_speed, self.sp_from, self.sp_to):
            sp.setFixedWidth(130)
        self.sp_spike.setFixedWidth(150)
        self.sp_ma.setFixedWidth(150)
        form.addRow("Automatica", W.hrow(self.tg_auto, self.cb_int, None))
        form.addRow("Solo tra le", W.hrow(self.sp_from, W.label("e le"), self.sp_to, None))
        form.addRow("Durata al contrario", self.sp_dur)
        form.addRow("Velocità", self.sp_speed)
        cl.add(form)
        self.btn_clsave = QPushButton("Salva pianificazione")
        self.btn_clsave.clicked.connect(self._clean_save)
        self.btn_clrun = QPushButton("Pulisci adesso", objectName="primary")
        self.btn_clrun.clicked.connect(self._clean_run)
        self.btn_clstop = QPushButton("Interrompi", objectName="danger")
        self.btn_clstop.clicked.connect(lambda: ctx.hub({"cmd": "fan.clean", "action": "stop"},
                                                        success="Interruzione richiesta"))
        cl.add(W.hrow(None, self.btn_clsave, self.btn_clstop, self.btn_clrun))
        self.add(cl)
        self.body.addStretch(1)

        ctx.snapshot.connect(self.on_snapshot)
        self.reload()

    # data -------------------------------------------------------------------------------------
    def activated(self):
        self.reload()

    def hub_state(self, ok):
        self._enable()

    def _enable(self):
        ok = self.ctx.hub_ok
        for w in (self.seg_mode, self.sl_manual, self.btn_use, self.btn_cnew, self.btn_cren, self.btn_cdel,
                  self.btn_csave, self.seg_interp, self.seg_src, self.btn_filter, self.btn_clsave,
                  self.btn_clrun, self.btn_clstop, self.tg_auto):
            w.setEnabled(ok)

    def reload(self, cfg=None):
        self.cfg = cfg or desktop.fan_config()
        c = self.cfg
        st = components.statuses().get("fan-control", {})
        self.svc_pill.set(st.get("summary", "missing"),
                          {"ok": "● servizio attivo", "off": "○ servizio fermo",
                           "failed": "✕ servizio in errore"}.get(st.get("summary")))
        self.seg_mode.set(c.get("mode"))
        self.sl_manual.blockSignals(True)
        self.sl_manual.setValue(round((c.get("manual_pwm") or 0) / 255 * 100))
        self.sl_manual.blockSignals(False)
        self.lb_manual.setText(f"{self.sl_manual.value()} %")
        self._mode_hint()
        curves = c.get("curves") or {}
        active = c.get("active_curve")
        sel = self.editing if self.editing in curves else active
        self.cb_curve.clear()
        for n in curves:
            self.cb_curve.addItem(("★ " if n == active else "") + n, n)
        i = self.cb_curve.findData(sel)
        self.cb_curve.setCurrentIndex(max(0, i))
        if not self.edit_dirty:
            self._load_curve(sel)
        self.seg_interp.set(c.get("curve_interpolation", "smooth"))
        self.editor.smooth = c.get("curve_interpolation", "smooth") == "smooth"
        self.seg_src.set(c.get("temp_source", "package"))
        self.sp_spike.setValue(int(c.get("spike_window", 15)))
        self.sp_ma.setValue(int(c.get("ma_window", 20)))
        # cleaner
        self.tg_auto.setChecked(bool(c.get("cleaner_auto")))
        h = float(c.get("cleaner_interval_hours", 168))
        idx = self.cb_int.findData(int(h)) if h.is_integer() else -1
        if idx < 0:
            self.cb_int.addItem(f"Ogni {h:g} ore", h)
            idx = self.cb_int.count() - 1
        self.cb_int.setCurrentIndex(idx)
        self.sp_from.setValue(int(c.get("cleaner_window_start", 8)))
        self.sp_to.setValue(int(c.get("cleaner_window_end", 22)))
        self.sp_dur.setValue(int(c.get("cleaner_duration", 30)))
        self.sp_speed.setValue(int(c.get("cleaner_speed", 37)) * 100)
        self._clean_status()
        self._enable()

    def _mode_hint(self):
        m = self.cfg.get("mode")
        self.mode_hint.setText({
            "curve": f"La curva «{self.cfg.get('active_curve')}» decide la velocità in base alla temperatura di controllo.",
            "auto": "Tabella ventole del firmware EC (pwm1_enable=2): non è leggibile né modificabile.",
            "max": "Ventole al massimo finché non cambi modalità.",
            "manual": "Velocità fissa: il demone la riscrive ogni 2 s.",
        }.get(m, ""))
        self.sl_manual.setEnabled(self.ctx.hub_ok and m == "manual")
        ov = desktop.fan_override()
        if ov:
            self.mode_hint.setText(self.mode_hint.text() + f"\n⚠ Modalità imposta dal profilo «{ov.get('profile')}»: "
                                   f"tornerà a «{ov.get('mode')}» quando il profilo attivo non toccherà più "
                                   "le ventole. Sceglierne una qui la rende la nuova base.")

    def _set(self, changes, msg="Impostazioni ventole salvate"):
        self.ctx.hub({"cmd": "fan.set", "changes": changes}, lambda r: self.reload(r["config"]),
                     success=msg, on_fail=lambda _: self.reload())

    def _mode(self, m):
        ch = {"mode": m}
        if m == "manual":
            ch["manual_pwm"] = round(self.sl_manual.value() / 100 * 255)
        self._set(ch, {"curve": "Modalità curva", "auto": "Ventole al firmware",
                       "max": "Ventole al massimo", "manual": "Velocità manuale"}[m])

    def _manual(self):
        self._set({"mode": "manual", "manual_pwm": round(self.sl_manual.value() / 100 * 255)},
                  f"Velocità manuale {self.sl_manual.value()} %")

    # curves -----------------------------------------------------------------------------------
    def _load_curve(self, name):
        pts = (self.cfg.get("curves") or {}).get(name) or self.cfg.get("curve") or []
        self.editing = name
        self.editor.set_points(pts)
        self.edit_dirty = False
        active = self.cfg.get("active_curve")
        self.active_lbl.setText("curva attiva" if name == active else f"attiva: {active}")
        self.btn_csave.setText("Salva punti")

    def _curve_selected(self, i):
        name = self.cb_curve.itemData(i)
        if self.edit_dirty and not self.ctx.confirm("Modifiche non salvate",
                                                    f"Scartare le modifiche a «{self.editing}»?"):
            self.cb_curve.setCurrentIndex(max(0, self.cb_curve.findData(self.editing)))
            return
        self._load_curve(name)

    def _curve_touched(self):
        self.edit_dirty = True
        self.btn_csave.setText("Salva punti •")

    def _curve_save(self):
        name = self.editing
        self.edit_dirty = False
        self._set({"save_curve": {"name": name, "points": self.editor.points,
                                  "activate": name == self.cfg.get("active_curve")}},
                  f"Curva «{name}» salvata")

    def _use_curve(self):
        name = self.editing
        ch = {"mode": "curve"}
        if self.edit_dirty:
            ch["save_curve"] = {"name": name, "points": self.editor.points, "activate": True}
            self.edit_dirty = False
        else:
            ch["active_curve"] = name
        self._set(ch, f"Curva «{name}» attiva")

    def _ask(self, title, text=""):
        name, ok = QInputDialog.getText(self, title, "Nome della curva:", text=text)
        return name.strip() if ok and name.strip() else None

    def _curve_new(self):
        name = self._ask("Nuova curva", f"{self.editing} 2" if self.editing else "")
        if name:
            self.editing = name
            self._set({"save_curve": {"name": name, "points": self.editor.points}}, f"Curva «{name}» creata")

    def _curve_rename(self):
        old = self.editing
        name = self._ask("Rinomina curva", old)
        if name and name != old:
            self.editing = name
            self._set({"rename_curve": {"old": old, "new": name}}, f"Rinominata in «{name}»")

    def _curve_delete(self):
        name = self.editing
        if self.ctx.confirm("Elimina curva", f"Eliminare la curva «{name}»?"):
            self.editing = None
            self._set({"delete_curve": name}, f"Curva «{name}» eliminata")

    # cleaning -------------------------------------------------------------------------------------
    def _clean_status(self):
        st = desktop.cleaner_state()
        running = desktop.cleaner_running(st)
        last = fmt_ts(st.get("last_run"))
        self.t_last.set(last, st.get("last_status", ""))
        if st.get("last_status"):
            self.t_last.sub.setStyleSheet(f"color: {(W.T.ok if st.get('last_ok') else W.T.warn).name()};")
        nxt = desktop.cleaner_next(self.cfg, st)
        self.t_next.set(fmt_ts(nxt) if nxt else ("disattivata" if not self.cfg.get("cleaner_auto") else "–"),
                        "rinviata se a batteria o sopra 75 °C" if nxt else "")
        if running:
            self.t_state.set(st.get("phase", "in corso"), f"{st.get('progress', 0)} %")
            self.prog.show()
            self.prog.setValue(int(st.get("progress", 0)))
        else:
            self.t_state.set("inattiva", "")
            self.prog.hide()
        self.btn_clstop.setVisible(running)
        self.btn_clrun.setEnabled(self.ctx.hub_ok and not running)
        return running

    def _clean_save(self):
        a, b = self.sp_from.value(), self.sp_to.value()
        self._set({"cleaner_auto": self.tg_auto.isChecked(), "cleaner_interval_hours": self.cb_int.currentData(),
                   "cleaner_window": [a, b], "cleaner_duration": self.sp_dur.value(),
                   "cleaner_speed": self.sp_speed.value() // 100}, "Pianificazione pulizia salvata")

    def _clean_run(self):
        s = self.ctx.last
        if s and s.ac_online is False:
            self.ctx.toast("A batteria la pulizia non è consigliata: collega il cavo.", "warn")
        self.ctx.hub({"cmd": "fan.clean", "action": "run", "duration": self.sp_dur.value(),
                      "speed": self.sp_speed.value() // 100},
                     success="Pulizia richiesta al servizio ventole")

    # live -----------------------------------------------------------------------------------------
    def on_snapshot(self, s):
        if not self.isVisible():
            return
        self.t_f1.set(s.fan1)
        self.t_f2.set(s.fan2)
        self.t_ct.set(s.control_temp, f"package {s.pkg_temp:.0f} · media core {s.core_mean:.0f}"
                      if s.pkg_temp is not None and s.core_mean is not None else "")
        pts = self.editor.points
        if self.cfg.get("mode") == "curve" and pts and s.control_temp is not None:
            active = (self.cfg.get("curves") or {}).get(self.cfg.get("active_curve")) or pts
            out = fanconf.curve_value(active, s.control_temp, self.editor.smooth)
            self.t_out.set(out, "stima dalla curva attiva")
            if self.editing == self.cfg.get("active_curve"):
                self.editor.set_marker(s.control_temp, fanconf.curve_value(pts, s.control_temp, self.editor.smooth))
            else:
                self.editor.set_marker(None, None)
        else:
            self.t_out.set(None, "")
            self.editor.set_marker(None, None)
        if self.btn_clstop.isVisible() or desktop.cleaner_state().get("state") == "running":
            self._clean_status()
