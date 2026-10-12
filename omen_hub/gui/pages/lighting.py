"""Luci e schermo: panel and keyboard brightness, their per-power-source
levels in powerdevil, the omen-fx brightness keys and idle dim, and quick
omen-fx actions (the full editor is the Studio luci sub-page)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
import shutil
import subprocess

from PyQt6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QHBoxLayout,
                             QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ... import desktop
from .. import widgets as W
from . import Page


class LightingPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Luci e schermo",
                         "Luminosità reale del pannello (registro EC tramite omen-backlight-sync), "
                         "retroilluminazione tastiera ed effetti omen-fx.")

        # now ------------------------------------------------------------------------
        now = W.Card("Adesso")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(14)
        self.sl_disp, self.sl_kbd = QSlider(Qt.Orientation.Horizontal), QSlider(Qt.Orientation.Horizontal)
        self.lb_disp, self.lb_kbd = W.label("", "muted"), W.label("", "muted")
        for lb in (self.lb_disp, self.lb_kbd):
            lb.setMinimumWidth(48)
        grid.addWidget(W.label("☀  Schermo"), 0, 0)
        grid.addWidget(self.sl_disp, 0, 1)
        grid.addWidget(self.lb_disp, 0, 2)
        grid.addWidget(W.label("⌨  Tastiera"), 1, 0)
        grid.addWidget(self.sl_kbd, 1, 1)
        grid.addWidget(self.lb_kbd, 1, 2)
        grid.setColumnStretch(1, 1)
        now.add(grid)
        now.add(W.label("La luminosità del pannello costa: 100 % → 30 % = −3,3 W a batteria.", "faint"))
        self.add(now)
        self._disp_max = 100
        self._kbd_max = 100
        # Apply while dragging, but at most every 120 ms
        self._pending = {}
        self._flush = QTimer(self, singleShot=True, interval=120, timeout=self._apply_pending)
        self.sl_disp.valueChanged.connect(lambda v: self._queue("disp", v))
        self.sl_kbd.valueChanged.connect(lambda v: self._queue("kbd", v))

        # per profile ----------------------------------------------------------------------
        pp = W.Card("Livelli per alimentazione",
                    "Applicati da powerdevil quando cambia l'alimentazione (Impostazioni → "
                    "Risparmio energetico). Al salvataggio powerdevil viene riavviato, perché "
                    "rilegge i livelli solo al cambio di profilo.")
        g = QGridLayout()
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(10)
        g.addWidget(W.label("", "statLabel"), 0, 0)
        for col, txt in enumerate(("Schermo", "", "Tastiera", ""), start=1):
            g.addWidget(W.label(txt, "statLabel"), 0, col)
        self.pd_rows = {}
        for r, (key, name) in enumerate(desktop.PD_PROFILES, start=1):
            t_d, t_k = W.Toggle(), W.Toggle()
            s_d, s_k = QSpinBox(), QSpinBox()
            for s in (s_d, s_k):
                s.setRange(0, 100)
                s.setSuffix(" %")
            t_d.toggled.connect(s_d.setEnabled)
            t_k.toggled.connect(s_k.setEnabled)
            g.addWidget(W.label(name), r, 0)
            g.addWidget(t_d, r, 1)
            g.addWidget(s_d, r, 2)
            g.addWidget(t_k, r, 3)
            g.addWidget(s_k, r, 4)
            self.pd_rows[key] = (t_d, s_d, t_k, s_k)
        g.setColumnStretch(5, 1)
        pp.add(g)
        self.btn_pd = QPushButton("Salva e riavvia powerdevil", objectName="primary")
        self.btn_pd.clicked.connect(self._save_pd)
        pp.add(W.hrow(W.label("Disattivato = powerdevil non tocca quel livello.", "faint"), None, self.btn_pd))
        self.add(pp)

        # brightness keys + idle dim (omen-fx's machine-wide switches) ---------------------
        keys = W.Card("Tasti luminosità per la tastiera",
                      "Alt + luminosità su/giù sposta la tastiera del 5 %, Shift+Alt dell'1 %, "
                      "tenendo premuto va a rampa. I tasti da soli continuano a regolare lo schermo.")
        self.keys_switch = W.Switch("Alt + i tasti luminosità regolano la tastiera")
        self.keys_switch.toggled.connect(self._toggle_keys)
        self.keys_pill = W.Pill()
        keys.add(W.hrow(self.keys_switch, None, self.keys_pill))
        self.keys_note = W.label("", "faint", wrap=True)
        keys.add(self.keys_note)
        self.add(keys)
        self._keys_loading = False

        idle = W.Card("Attenua tastiera e barra quando sei via",
                      "Dopo un po' senza tastiera né mouse omen-fx abbassa tastiera e barra; "
                      "il primo tasto le riporta al livello di sopra, che resta il riferimento.")
        g = QGridLayout()
        g.setHorizontalSpacing(48)
        self.idle_cols = {"ac": IdleColumn("Rete"), "battery": IdleColumn("Batteria")}
        g.addWidget(self.idle_cols["ac"], 0, 0)
        g.addWidget(self.idle_cols["battery"], 0, 1)
        g.setColumnStretch(2, 1)
        idle.add(g)
        self.idle_note = W.label("", "faint", wrap=True)
        self.btn_idle = QPushButton("Salva attenuazione", objectName="primary")
        self.btn_idle.clicked.connect(self._save_idle)
        idle.add(W.hrow(self.idle_note, None, self.btn_idle))
        self.add(idle)

        # omen-fx ---------------------------------------------------------------------------
        fx = W.Card("omen-fx · barra luminosa e tastiera RGB",
                    "Qui le azioni rapide; temi, zone disegnabili, aspetto predefinito ed "
                    "effetti degli avvisi si creano e modificano nello Studio luci.")
        self.btn_gui = QPushButton("✦  Studio luci  →", objectName="primary")
        self.btn_gui.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gui.clicked.connect(lambda: self.ctx.window.go_name("fxstudio"))
        fx.header.addWidget(self.btn_gui, 0, Qt.AlignmentFlag.AlignTop)
        self.fx_pill = W.Pill()
        self.fx_info = W.label("", "muted", wrap=True)
        fx.add(W.hrow(self.fx_pill, self.fx_info, None))
        self.cb_theme = QComboBox()
        self.cb_theme.setMinimumWidth(220)
        self.btn_theme = QPushButton("Applica tema", objectName="primary")
        self.btn_theme.clicked.connect(self._apply_theme)
        self.theme_lbl = W.label("", "faint")
        fx.add(W.hrow(W.label("Tema"), self.cb_theme, self.btn_theme, self.theme_lbl, None))
        self.cb_effect = QComboBox()
        self.cb_effect.setMinimumWidth(220)
        self.seg_target = W.Segmented([("both", "Entrambe"), ("bar", "Barra"), ("keys", "Tastiera")])
        self.seg_target.set("both")
        self.btn_play = QPushButton("▶  Prova effetto")
        self.btn_play.clicked.connect(self._play)
        self.btn_stop = QPushButton("■  Ferma tutto")
        self.btn_stop.clicked.connect(lambda: self._fx({"cmd": "stop"}, "Effetti fermati"))
        fx.add(W.hrow(W.label("Effetto"), self.cb_effect, self.seg_target, self.btn_play, self.btn_stop, None))
        self.add(fx)
        self.body.addStretch(1)

    # --------------------------------------------------------------------------------------
    def activated(self):
        def read():
            return desktop.brightness(), desktop.kbd_brightness(), desktop.powerdevil_profiles()
        W.run_async(read, self._show_levels, lambda m: self.ctx.toast(m, "err"))
        self._refresh_fx()
        self._load_keys()

    def _show_levels(self, res):
        disp, kbd, pd = res
        for sl, val, lb, attr in ((self.sl_disp, disp, self.lb_disp, "_disp_max"),
                                  (self.sl_kbd, kbd, self.lb_kbd, "_kbd_max")):
            sl.blockSignals(True)
            if val:
                setattr(self, attr, val[1])
                sl.setRange(0, val[1])
                sl.setValue(val[0])
                lb.setText(f"{round(val[0] / val[1] * 100)} %")
                sl.setEnabled(True)
            else:
                sl.setEnabled(False)
                lb.setText("n/d")
            sl.blockSignals(False)
        for key, (t_d, s_d, t_k, s_k) in self.pd_rows.items():
            v = pd.get(key, {})
            t_d.setChecked(v.get("display_on", False))
            s_d.setValue(v.get("display", 50))
            s_d.setEnabled(t_d.isChecked())
            t_k.setChecked(v.get("kbd_on", False))
            s_k.setValue(v.get("kbd", 50))
            s_k.setEnabled(t_k.isChecked())

    def _queue(self, which, v):
        mx = self._disp_max if which == "disp" else self._kbd_max
        (self.lb_disp if which == "disp" else self.lb_kbd).setText(f"{round(v / (mx or 1) * 100)} %")
        self._pending[which] = v
        if not self._flush.isActive():
            self._flush.start()

    def _apply_pending(self):
        pend, self._pending = self._pending, {}
        try:
            if "disp" in pend:
                desktop.set_brightness(pend["disp"])
            if "kbd" in pend:
                desktop.set_kbd_brightness(pend["kbd"])
        except Exception as exc:
            self.ctx.toast(str(exc), "err")

    def _save_pd(self):
        values = {k: {"display_on": t_d.isChecked(), "display": s_d.value(),
                      "kbd_on": t_k.isChecked(), "kbd": s_k.value()}
                  for k, (t_d, s_d, t_k, s_k) in self.pd_rows.items()}
        W.run_async(lambda: desktop.save_powerdevil_profiles(values),
                    lambda _: (self.ctx.toast("Livelli salvati, powerdevil riavviato", "ok"), self.activated()),
                    lambda m: self.ctx.toast(m, "err"))

    # omen-fx ---------------------------------------------------------------------------------
    def _fx(self, req, ok_msg=None, then=None):
        def done(r):
            if ok_msg:
                self.ctx.toast(ok_msg, "ok")
            if then:
                then(r)
        W.run_async(lambda: desktop.fx(req), done, lambda m: self.ctx.toast(f"omen-fx: {m}", "err"))

    def _refresh_fx(self):
        def read():
            return desktop.fx({"cmd": "status"}), desktop.fx({"cmd": "config", "action": "get"})

        def show(res):
            st, cfg = res
            s = st.get("status", {})
            self.fx_pill.set("ok" if s.get("available") else "warn",
                             "● omen-fxd attivo" if s.get("available") else "▲ barra non disponibile")
            kb = "tastiera per-tasto " + ("ok" if s.get("keyboard_available") else "assente")
            idle = s.get("idle") or {}
            self.fx_info.setText(f"{kb} · {s.get('lamps', 0)} LED · livello {s.get('master_brightness', '?')} % · "
                                 f"attenuazione inattività {'attiva' if idle.get('enabled') else 'spenta'}")
            self.cb_effect.clear()
            self.cb_effect.addItems(st.get("effects", []))
            self.cb_theme.clear()
            self.cb_theme.addItems(sorted((cfg.get("themes") or {}).keys()))
            act = cfg.get("active_theme")
            if act:
                self.cb_theme.setCurrentText(act)
            self.theme_lbl.setText(f"attivo: {act}" + (" (modificato)" if cfg.get("theme_modified") else "")
                                   if act else "")
            # The daemon answers per source; an older one answers with one flat
            # table, which then stands for both.
            idle_cfg = cfg.get("idle") or {}
            for key, col in self.idle_cols.items():
                col.load(idle_cfg.get(key) or {} if ("ac" in idle_cfg or "battery" in idle_cfg)
                         else idle_cfg)
            self.idle_note.setText("" if idle.get("available", True) else
                                   "omen-fxd non può leggere /dev/input, quindi non sa quando sei "
                                   "via: l'attenuazione resta spenta.")
            for w in (self.btn_theme, self.btn_play, self.btn_stop, self.cb_theme, self.cb_effect,
                      self.btn_idle, *self.idle_cols.values()):
                w.setEnabled(True)

        def failed(msg):
            self.fx_pill.set("missing", "– omen-fxd non raggiungibile")
            self.fx_info.setText(msg)
            for w in (self.btn_theme, self.btn_play, self.btn_stop, self.cb_theme, self.cb_effect,
                      self.btn_idle, *self.idle_cols.values()):
                w.setEnabled(False)
        W.run_async(read, show, failed)

    def _apply_theme(self):
        name = self.cb_theme.currentText()
        if name:
            self._fx({"cmd": "config", "action": "apply_theme", "name": name}, f"Tema «{name}» applicato",
                     lambda _: self._refresh_fx())

    def _play(self):
        name = self.cb_effect.currentText()
        if name:
            self._fx({"cmd": "play", "effect": name, "target": self.seg_target.value() or "both"})

    # brightness keys (a *user* service: the daemons have no business in the session) ----
    def _load_keys(self):
        def read():
            _, out = _systemctl("is-enabled", KEYS_UNIT)
            active, _ = _systemctl("is-active", KEYS_UNIT)
            return (out.splitlines()[0].strip() if out else ""), active == 0

        def show(res):
            known, active = res
            # "not-found" is what systemd says for a unit that is not installed;
            # an empty answer means systemctl itself is missing or failed.
            if known in ("", "not-found", "systemctl non disponibile"):
                self.keys_switch.setEnabled(False)
                self.keys_pill.set("missing")
                self.keys_note.setText("Servizio non installato: rilancia install.sh di omen-fx.")
                return
            self.keys_switch.setEnabled(True)
            self._keys_loading = True
            self.keys_switch.setChecked(known == "enabled")
            self._keys_loading = False
            self.keys_pill.set("ok" if active else "off")
            self.keys_note.setText(f"{KEYS_UNIT} (servizio utente)")
        W.run_async(read, show, lambda m: self.ctx.toast(m, "err"))

    def _toggle_keys(self, on):
        if self._keys_loading:
            return
        self.keys_switch.setEnabled(False)

        def act():
            code, out = _systemctl("enable" if on else "disable", "--now", KEYS_UNIT)
            if code != 0:
                raise RuntimeError(f"{KEYS_UNIT}: {out.splitlines()[0] if out else code}")

        def done(_):
            self.ctx.toast("Tasti luminosità " + ("attivati" if on else "disattivati"), "ok")
            self._load_keys()

        def failed(msg):
            self.ctx.toast(msg, "err")
            self._load_keys()
        W.run_async(act, done, failed)

    def _save_idle(self):
        req = {"cmd": "config", "action": "save_settings",
               "idle": {k: c.values() for k, c in self.idle_cols.items()}}
        self._fx(req, None, lambda r: self.ctx.toast(f"Attenuazione salvata in {r.get('file')}", "ok"))


KEYS_UNIT = "omen-fx-brightnessd.service"


def _systemctl(*args: str) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 1, "systemctl non disponibile"
    try:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


class IdleColumn(QWidget):
    """The idle-dim knobs for one power source (omen-fxd's [idle.ac|battery])."""

    def __init__(self, title: str):
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)
        col.addWidget(W.label(title.upper(), "statLabel"))
        self.enabled = W.Switch("Attenua")
        self.enabled.toggled.connect(self._toggle)
        col.addWidget(self.enabled)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setSingleStep(5)
        form.addRow("Dopo", self.timeout)
        self.brightness = QSpinBox()
        self.brightness.setRange(0, 100)
        self.brightness.setSuffix(" %")
        self.brightness.setToolTip(
            "Quanta luce resta una volta attenuata. 0 le spegne.\n"
            "Non tocca il tuo livello: lo slider della luminosità resta dov'è\n"
            "e il primo tasto premuto torna subito lì.")
        form.addRow("Fino a", self.brightness)
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0.0, 30.0)
        self.fade.setSingleStep(0.5)
        self.fade.setDecimals(1)
        self.fade.setSuffix(" s")
        self.fade.setToolTip("Quanto dura l'attenuazione. Il risveglio è immediato.")
        form.addRow("Dissolvenza", self.fade)
        col.addLayout(form)
        self.wake = W.Switch(
            "Gli avvisi la risvegliano",
            "Mentre un avviso suona su una superficie attenuata, la superficie\n"
            "torna piena per l'avviso e si riattenua dopo. Il tuo livello vale\n"
            "comunque: con lo slider della tastiera a 0 non si accende nulla.")
        col.addWidget(self.wake)

    def _toggle(self, on: bool) -> None:
        for widget in (self.timeout, self.brightness, self.fade, self.wake):
            widget.setEnabled(on)

    def load(self, idle: dict) -> None:
        self.enabled.setChecked(bool(idle.get("enabled")))
        self.timeout.setValue(int(idle.get("timeout", 60)))
        self.brightness.setValue(int(idle.get("brightness", 20)))
        self.fade.setValue(float(idle.get("fade_ms", 1500)) / 1000.0)
        self.wake.setChecked(bool(idle.get("wake_for_alerts")))
        self._toggle(self.enabled.isChecked())

    def values(self) -> dict:
        return {
            "enabled": self.enabled.isChecked(),
            "timeout": self.timeout.value(),
            "brightness": self.brightness.value(),
            "fade_ms": int(round(self.fade.value() * 1000)),
            "wake_for_alerts": self.wake.isChecked(),
        }
