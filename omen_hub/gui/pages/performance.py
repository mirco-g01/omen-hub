"""Prestazioni: profile library (OMEN Gaming Hub style) applied through
auto-cpufreq, one profile for mains and one for battery."""

from __future__ import annotations

import copy
import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel, QGridLayout, QHBoxLayout, QInputDialog,
                             QLineEdit, QListWidget, QListWidgetItem, QSpinBox, QVBoxLayout,
                             QWidget, QColorDialog, QPushButton)
from PyQt6.QtGui import QColor, QIcon, QPixmap, QPainter

from ... import components, cpuconf, desktop, paths, profiles
from .. import widgets as W
from . import Page

EPP_LABELS = {"performance": "Prestazioni", "balance_performance": "Bilanciato (prestazioni)",
              "balance_power": "Bilanciato (risparmio)", "power": "Risparmio", "default": "Default firmware"}
PLATFORM_LABELS = {"low-power": "Silenzioso", "balanced": "Bilanciato", "performance": "Prestazioni"}


def swatch(color: str) -> QIcon:
    pm = QPixmap(14, 14)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 14, 14, 4, 4)
    p.end()
    return QIcon(pm)


class PerformancePage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Prestazioni",
                         "Profili CPU/GPU come in OMEN Gaming Hub. auto-cpufreq applica da sé "
                         "il profilo giusto quando attacchi o stacchi il cavo.")
        self.needs_hub_banner()
        self.store = None
        self.current = None       # name being edited
        self.dirty = False
        s = ctx.sampler
        self.freq_range = ((s.hw_min or 800000) // 1000, max(s.hw_max_p or 0, s.hw_max_e or 0) // 1000 or 5500)

        # assignment ---------------------------------------------------------
        a = W.Card("Assegnazione", "Quale profilo usare per ciascuna alimentazione.")
        self.cb_ac, self.cb_bat = QComboBox(), QComboBox()
        self.btn_assign = QPushButton("Applica", objectName="primary")
        self.btn_assign.clicked.connect(self.save_assign)
        a.add(W.hrow(W.label("🔌  Sotto rete"), self.cb_ac, W.label("   🔋  A batteria"), self.cb_bat,
                     None, self.btn_assign))
        self.add(a)

        # library + editor -----------------------------------------------------
        lib = W.Card("Libreria profili")
        row = QHBoxLayout()
        row.setSpacing(18)
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.setFixedWidth(230)
        self.list.currentItemChanged.connect(self._select)
        left.addWidget(self.list, 1)
        self.btn_new = QPushButton("Nuovo")
        self.btn_dup = QPushButton("Duplica")
        self.btn_ren = QPushButton("Rinomina")
        self.btn_del = QPushButton("Elimina", objectName="danger")
        self.btn_new.clicked.connect(lambda: self._new(False))
        self.btn_dup.clicked.connect(lambda: self._new(True))
        self.btn_ren.clicked.connect(self._rename)
        self.btn_del.clicked.connect(self._delete)
        g = QGridLayout()
        for i, b in enumerate((self.btn_new, self.btn_dup, self.btn_ren, self.btn_del)):
            g.addWidget(b, i // 2, i % 2)
        left.addLayout(g)
        row.addLayout(left)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.ed_desc = QLineEdit()
        self.btn_color = QPushButton("Colore")
        self.btn_color.clicked.connect(self._pick_color)
        self.seg_gov = W.Segmented([("powersave", "powersave"), ("performance", "performance")])
        self.cb_epp = QComboBox()
        for k in cpuconf.EPPS:
            self.cb_epp.addItem(EPP_LABELS[k], k)
        self.seg_turbo = W.Segmented([("never", "Mai"), ("auto", "Automatico"), ("always", "Sempre")])
        choices = s.sample().platform_choices or ["low-power", "balanced", "performance"]
        self.seg_plat = W.Segmented([(c, PLATFORM_LABELS.get(c, c)) for c in choices])
        def ghz(v):
            return f"{v / 1000:.1f} GHz".replace(".", ",")

        def mult(r):
            return f"×{r} · {r / 10:.1f} GHz".replace(".", ",")
        lo_f, hi_f = self.freq_range
        self.sl_min = W.SliderField(lo_f, hi_f, lo_f, ghz, step=100)
        self.sl_max = W.SliderField(lo_f, hi_f, hi_f, ghz, step=100)
        self.cb_fan = QComboBox()
        form.addRow("Descrizione", W.hrow(self.ed_desc, self.btn_color))
        form.addRow("Governor", self.seg_gov)
        form.addRow("Preferenza energetica (EPP)", self.cb_epp)
        form.addRow("Turbo boost", self.seg_turbo)
        form.addRow("Profilo piattaforma", self.seg_plat)
        form.addRow("Frequenza minima", self.sl_min)
        form.addRow("Frequenza massima", self.sl_max)
        form.addRow("Ventole", self.cb_fan)

        # Ranges start from the values measured on this machine and are
        # replaced by the firmware's own as soon as omen-hubd reports them.
        self.sl_pl1 = W.SliderField(10, 170, 170, lambda v: f"{v} W")
        self.sl_pl2 = W.SliderField(10, 170, 170, lambda v: f"{v} W")
        self.sl_gpu = W.SliderField(300, 3090, 3090, lambda v: f"{v} MHz", step=50)
        self.sl_rp = W.SliderField(8, 52, 52, mult)
        self.sl_re = W.SliderField(8, 45, 45, mult)
        self.tg_bins, self.rp_bins, self.bins_box, self.bin_labels = self._bins_ui(
            [52, 52, 50, 50, 50, 50, 50, 50], mult, self.sl_rp)
        self.tg_ebins, self.re_bins, self.ebins_box, self.ebin_labels = self._bins_ui([45] * 8, mult, self.sl_re)
        form.addRow(W.label("Limiti di potenza", bold=True))
        form.addRow("CPU PL1 (sostenuto)", self.sl_pl1)
        form.addRow("CPU PL2 (picco)", self.sl_pl2)
        form.addRow("GPU · clock massimo", self.sl_gpu)
        form.addRow(W.label("Moltiplicatori massimi", bold=True))
        for name, sl, tg, box in (("P-core", self.sl_rp, self.tg_bins, self.bins_box),
                                  ("E-core", self.sl_re, self.tg_ebins, self.ebins_box)):
            form.addRow(name, sl)
            form.addRow("", W.hrow(tg, W.label("diverso per numero di core attivi", "muted"), None))
            form.addRow("", box)
        self.limits_info = None
        right = QVBoxLayout()
        right.addLayout(form)
        self.hints = W.label("", "faint", wrap=True)
        right.addWidget(self.hints)
        self.btn_save = QPushButton("Salva e applica", objectName="primary")
        self.btn_save.clicked.connect(self.save_profile)
        self.btn_revert = QPushButton("Annulla modifiche", objectName="ghost")
        self.btn_revert.clicked.connect(lambda: self._load_editor(self.current))
        right.addLayout(W.hrow(None, self.btn_revert, self.btn_save))
        right.addStretch(1)
        row.addLayout(right, 1)
        lib.add(row)
        self.add(lib)

        for sig in (self.ed_desc.textEdited, self.cb_epp.currentIndexChanged, self.cb_fan.currentIndexChanged):
            sig.connect(self._touched)
        for sl in (self.sl_min, self.sl_max, self.sl_pl1, self.sl_pl2, self.sl_gpu, self.sl_rp, self.sl_re,
                   *self.rp_bins, *self.re_bins):
            sl.changed.connect(self._touched)
        self.sl_pl1.changed.connect(lambda: self._keep_order(self.sl_pl1, self.sl_pl2, True))
        self.sl_pl2.changed.connect(lambda: self._keep_order(self.sl_pl1, self.sl_pl2, False))
        self.sl_min.changed.connect(lambda: self._keep_order(self.sl_min, self.sl_max, True))
        self.sl_max.changed.connect(lambda: self._keep_order(self.sl_min, self.sl_max, False))
        self.tg_bins.toggled.connect(self._touched)
        self.tg_ebins.toggled.connect(self._touched)
        for seg in (self.seg_gov, self.seg_turbo, self.seg_plat):
            seg.changed.connect(self._touched)

        # live state -------------------------------------------------------------
        live = W.Card("In vigore adesso", "Valori letti da sysfs: verificano che auto-cpufreq abbia applicato il profilo.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        self.l_gov = W.StatTile("Governor", "", 13)
        self.l_epp = W.StatTile("EPP", "", 13)
        self.l_turbo = W.StatTile("Turbo", "", 13)
        self.l_plat = W.StatTile("Piattaforma", "", 13)
        self.l_freq = W.StatTile("Limiti frequenza", "", 13)
        self.l_lim = W.StatTile("PL1/PL2 · moltiplicatori", "", 13)
        for i, t in enumerate((self.l_gov, self.l_epp, self.l_turbo, self.l_plat, self.l_freq, self.l_lim)):
            grid.addWidget(t, 0, i)
        live.add(grid)
        self.add(live)

        # powertop -------------------------------------------------------------------
        pt = W.Card("powertop", "Auto-tune al boot; il drop-in rimette la tastiera OMEN fuori "
                                "dall'autosospensione USB subito dopo.")
        self.pt_pill = W.Pill()
        self.btn_pt = QPushButton("Riesegui auto-tune")
        self.btn_pt.clicked.connect(lambda: ctx.hub({"cmd": "powertop.tune"}, success="powertop auto-tune eseguito",
                                                    timeout=150))
        pt.add(W.hrow(self.pt_pill, None, self.btn_pt))
        self.add(pt)
        self.body.addStretch(1)

        ctx.snapshot.connect(self.on_snapshot)
        self.reload()

    # data ------------------------------------------------------------------------------
    def activated(self):
        if not self.dirty:
            self.reload()
        self._load_limits_info()
        st = components.statuses().get("powertop", {})
        self.pt_pill.set(st.get("summary", "off"))

    def hub_state(self, ok):
        self.reload()
        self._load_limits_info()

    def reload(self):
        if self.ctx.hub_ok:
            self.ctx.hub({"cmd": "profiles.get"}, lambda r: self._set_store(r["store"]))
        else:
            self._set_store(profiles.load(paths.PROFILES, paths.AUTOCPUFREQ_CONF))

    def _set_store(self, store):
        self.store = copy.deepcopy(store)
        keep = self.current if self.current in store["profiles"] else store["assign"].get("ac")
        self.list.blockSignals(True)
        self.list.clear()
        for name, p in store["profiles"].items():
            tags = [lab for src, lab in (("ac", "rete"), ("battery", "batteria")) if store["assign"].get(src) == name]
            it = QListWidgetItem(swatch(p.get("color", "#888")), name + (f"   · {', '.join(tags)}" if tags else ""))
            it.setData(Qt.ItemDataRole.UserRole, name)
            self.list.addItem(it)
            if name == keep:
                self.list.setCurrentItem(it)
        self.list.blockSignals(False)
        for cb, src in ((self.cb_ac, "ac"), (self.cb_bat, "battery")):
            cb.blockSignals(True)
            cb.clear()
            for name, p in store["profiles"].items():
                cb.addItem(swatch(p.get("color", "#888")), name)
            cb.setCurrentText(store["assign"].get(src, ""))
            cb.blockSignals(False)
        curves = list((desktop.fan_config().get("curves") or {}).keys())
        self.cb_fan.blockSignals(True)
        self.cb_fan.clear()
        self.cb_fan.addItem("Non toccare le ventole", None)
        for c in curves:
            self.cb_fan.addItem(f"Curva «{c}»", {"mode": "curve", "curve": c})
        self.cb_fan.addItem("Auto (tabella BIOS)", {"mode": "auto", "curve": None})
        self.cb_fan.addItem("Massima velocità", {"mode": "max", "curve": None})
        self.cb_fan.blockSignals(False)
        self._load_editor(keep)
        self._enable()

    def _enable(self):
        ok = self.ctx.hub_ok
        for w in (self.btn_assign, self.btn_save, self.btn_new, self.btn_dup, self.btn_ren,
                  self.btn_del, self.btn_pt):
            w.setEnabled(ok)

    def _select(self, item, _prev=None):
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if self.dirty and self.current and name != self.current:
            if not self.ctx.confirm("Modifiche non salvate",
                                    f"Le modifiche a «{self.current}» andranno perse. Continuare?"):
                self.list.blockSignals(True)
                for i in range(self.list.count()):
                    if self.list.item(i).data(Qt.ItemDataRole.UserRole) == self.current:
                        self.list.setCurrentRow(i)
                self.list.blockSignals(False)
                return
        self._load_editor(name)

    def _load_editor(self, name):
        if not self.store or name not in self.store["profiles"]:
            return
        self.current = name
        p = self.store["profiles"][name]
        widgets = (self.ed_desc, self.cb_epp, self.cb_fan, self.tg_bins, self.tg_ebins)
        for w in widgets:
            w.blockSignals(True)
        self.ed_desc.setText(p.get("description", ""))
        self.seg_gov.set(p.get("governor"))
        self.cb_epp.setCurrentIndex(max(0, self.cb_epp.findData(p.get("epp"))))
        self.seg_turbo.set(p.get("turbo"))
        self.seg_plat.set(p.get("platform_profile"))
        self.sl_min.set_value(p.get("min_freq"))
        self.sl_max.set_value(p.get("max_freq"))
        fan = p.get("fan")
        idx = 0
        for i in range(self.cb_fan.count()):
            if self.cb_fan.itemData(i) == fan:
                idx = i
        self.cb_fan.setCurrentIndex(idx)
        self._color = p.get("color", "#888888")
        self.btn_color.setIcon(swatch(self._color))
        self.sl_pl1.set_value(p.get("pl1"))
        self.sl_pl2.set_value(p.get("pl2"))
        self.sl_gpu.set_value(p.get("gpu_clock"))
        for field, main, tg, box, bins in (("ratio_p", self.sl_rp, self.tg_bins, self.bins_box, self.rp_bins),
                                           ("ratio_e", self.sl_re, self.tg_ebins, self.ebins_box, self.re_bins)):
            rp = p.get(field)
            if isinstance(rp, int):
                rp = [rp] * 8
            uniform = rp is None or len(set(rp)) == 1
            tg.setChecked(not uniform)
            box.setVisible(not uniform)
            main.setEnabled(uniform)
            main.set_value(rp[0] if rp and uniform else None)
            for i, sl in enumerate(bins):
                sl.set_value(rp[i] if rp else None)
        for w in widgets:
            w.blockSignals(False)
        self.dirty = False
        self._update_hints()

    def _editor_profile(self) -> dict:
        return {
            "description": self.ed_desc.text(),
            "governor": self.seg_gov.value() or "powersave",
            "epp": self.cb_epp.currentData(),
            "turbo": self.seg_turbo.value() or "auto",
            "platform_profile": self.seg_plat.value(),
            "min_freq": self.sl_min.value(),
            "max_freq": self.sl_max.value(),
            "fan": self.cb_fan.currentData(),
            "color": self._color,
            "pl1": self.sl_pl1.value(), "pl2": self.sl_pl2.value(), "gpu_clock": self.sl_gpu.value(),
            "ratio_p": self._ratios(self.sl_rp, self.tg_bins, self.rp_bins),
            "ratio_e": self._ratios(self.sl_re, self.tg_ebins, self.re_bins),
        }

    @staticmethod
    def _keep_order(lo, hi, lo_moved):
        """PL1 <= PL2 and min <= max: dragging one pushes the other."""
        a = lo.value() if lo.value() is not None else lo.default
        b = hi.value() if hi.value() is not None else hi.default
        if a is None or b is None or a <= b:
            return
        if lo_moved:
            hi.set_value(a)
        else:
            lo.set_value(b)

    def _bins_ui(self, defaults, fmt, main):
        """Toggle + one slider per active-core bin, kept non-increasing:
        the CPU applies the lowest limit among the bins in effect."""
        tg = W.Toggle()
        sliders = [W.SliderField(8, d, d, fmt) for d in defaults]
        box = QWidget()
        bl = QFormLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setVerticalSpacing(4)
        labels = []
        for i, sl in enumerate(sliders):
            lab = W.label(f"{i + 1} core attiv{'o' if i == 0 else 'i'}", "faint")
            labels.append(lab)
            bl.addRow(lab, sl)
            sl.changed.connect(lambda i=i, sls=sliders: self._push_bins(sls, i))
        box.setVisible(False)

        def mode(on):
            box.setVisible(on)
            main.setEnabled(not on)
            if on and main.value() is not None:
                for sl in sliders:
                    sl.set_value(min(main.value(), sl.hi))
        tg.toggled.connect(mode)
        return tg, sliders, box, labels

    @staticmethod
    def _push_bins(sliders, i):
        def eff(sl):
            return sl.value() if sl.value() is not None else sl.default
        v = eff(sliders[i])
        for sl in sliders[i + 1:]:          # more active cores: never above
            if eff(sl) > v:
                sl.set_value(min(v, sl.hi))
        for sl in sliders[:i]:              # fewer active cores: never below
            if eff(sl) < v:
                sl.set_value(v)

    @staticmethod
    def _ratios(main, tg, sliders):
        if tg.isChecked():
            vals = [sl.value() for sl in sliders]
            return None if all(v is None for v in vals) else vals
        return [main.value()] * 8 if main.value() is not None else None

    @staticmethod
    def _bin_labels(labels, sliders, bins, fw):
        prev, seen = 0, set()
        for i, (lab, sl) in enumerate(zip(labels, sliders)):
            n = bins[i] if i < len(bins) else i + 1
            dup = n in seen or n <= prev and i > 0
            seen.add(n)
            lab.setText(f"{n} core attiv{'o' if n == 1 else 'i'}" if n - prev <= 1
                        else f"{prev + 1}–{n} core attivi")
            sl.set_range(8, fw[i], fw[i])
            lab.setVisible(not dup)       # a repeated bin (e.g. 12,12) is the same group
            sl.setVisible(not dup)
            if not dup:
                prev = n

    def _load_limits_info(self):
        if not self.ctx.hub_ok:
            return

        def show(r):
            self.limits_info = r
            d = r.get("defaults", {})
            pl = d.get("pl")
            if pl:
                self.sl_pl1.set_range(10, pl[0], pl[0])
                self.sl_pl2.set_range(10, pl[1], pl[1])
            g = d.get("gpu")
            if g and g.get("clock_max"):
                cm = int(g["clock_max"])
                self.sl_gpu.set_range(300, cm, cm)
            tp, te = d.get("trl_p"), d.get("trl_e")
            if tp:
                self.sl_rp.set_range(8, max(tp), max(tp))
                self._bin_labels(self.bin_labels, self.rp_bins, r.get("p_bins") or list(range(1, 9)), tp)
            if te:
                self.sl_re.set_range(8, max(te), max(te))
                self._bin_labels(self.ebin_labels, self.re_bins, r.get("e_bins") or list(range(1, 9)), te)
        self.ctx.hub({"cmd": "limits.info"}, show)

    def _touched(self, *_):
        self.dirty = True
        self._update_hints()

    def _update_hints(self):
        p = self._editor_profile()
        h = []
        if p["max_freq"]:
            h.append("⚠ Sui P-core (Arrow Lake, HWP) la frequenza massima viene scritta ma non "
                     "rispettata (04/09): per limitarli usa il moltiplicatore, che invece rispettano.")
        if p["turbo"] == "auto":
            h.append("ⓘ Turbo automatico: auto-cpufreq lo accende solo sopra ~20 % di carico totale "
                     "(4 core su 20); un lavoro single-thread resta a ~2,4 GHz.")
        if p["governor"] == "performance":
            h.append("⚠ Governor performance: intel_pstate rifiuta l'EPP e auto-cpufreq riempie il "
                     "journal di «Device or resource busy» ogni 5 s. powersave + EPP performance "
                     "dà le stesse frequenze.")
        if p.get("ratio_p") or p.get("ratio_e"):
            h.append("ⓘ Moltiplicatori: rispettati da P-core ed E-core (verificato il 12/10). Per gruppo "
                     "valgono solo a scendere (più core attivi → limite uguale o più basso). Sopra il "
                     "valore di fabbrica la CPU li ignora: niente overclock da qui.")
        if p.get("gpu_clock"):
            h.append("ⓘ Il driver non permette di cambiare i watt della GPU (il 115↔140 W è Dynamic "
                     "Boost): il tetto al clock fa da limite di potenza (1200 MHz ≈ 75 W invece di 139 W "
                     "sotto carico, misurato). Si applica quando la GPU è già "
                     "accesa (l'hub non la sveglia) e viene riapplicato a ogni suo risveglio.")
        if p["platform_profile"] == "performance":
            h.append("ⓘ Platform performance abilita il cTGP della GPU (fino a ~140 W).")
        self.hints.setText("\n".join(h))

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Colore del profilo")
        if c.isValid():
            self._color = c.name()
            self.btn_color.setIcon(swatch(self._color))
            self._touched()

    # actions ------------------------------------------------------------------------------
    def _push(self, store, msg):
        self.ctx.hub({"cmd": "profiles.save", "store": store},
                     lambda r: (self._set_store(store), self.ctx.toast(f"{msg} · {r.get('output', '')}", "ok"),
                                self._load_limits_info()))

    def save_profile(self):
        if not self.current:
            return
        store = copy.deepcopy(self.store)
        store["profiles"][self.current] = self._editor_profile()
        self.dirty = False
        self._push(store, f"«{self.current}» salvato")

    def save_assign(self):
        store = copy.deepcopy(self.store)
        store["assign"] = {"ac": self.cb_ac.currentText(), "battery": self.cb_bat.currentText()}
        self._push(store, "Assegnazione applicata")

    def _ask_name(self, title, default=""):
        name, ok = QInputDialog.getText(self, title, "Nome:", text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if not profiles.NAME_RE.fullmatch(name):
            self.ctx.toast("Nome non valido (max 40 caratteri: lettere, numeri, spazi, . ( ) + -)", "err")
            return None
        if name in self.store["profiles"]:
            self.ctx.toast(f"Esiste già un profilo «{name}»", "err")
            return None
        return name

    def _new(self, duplicate):
        base = self._editor_profile() if duplicate else copy.deepcopy(profiles.PRESETS["Bilanciato"])
        name = self._ask_name("Duplica profilo" if duplicate else "Nuovo profilo",
                              f"{self.current} (copia)" if duplicate else "")
        if not name:
            return
        if not duplicate:
            base["description"] = ""
        store = copy.deepcopy(self.store)
        store["profiles"][name] = base
        self.current = name
        self._push(store, f"«{name}» creato")

    def _rename(self):
        old = self.current
        name = self._ask_name("Rinomina profilo", old)
        if not name:
            return
        store = copy.deepcopy(self.store)
        store["profiles"] = {(name if k == old else k): v for k, v in store["profiles"].items()}
        store["assign"] = {k: (name if v == old else v) for k, v in store["assign"].items()}
        self.current = name
        self._push(store, f"Rinominato in «{name}»")

    def _delete(self):
        name = self.current
        if name in self.store["assign"].values():
            self.ctx.toast("Il profilo è assegnato a rete o batteria: assegnane un altro prima.", "warn")
            return
        if len(self.store["profiles"]) <= 1 or not self.ctx.confirm("Elimina profilo", f"Eliminare «{name}»?"):
            return
        store = copy.deepcopy(self.store)
        del store["profiles"][name]
        self.current = None
        self._push(store, f"«{name}» eliminato")

    # live -------------------------------------------------------------------------------------
    def on_snapshot(self, s):
        if not self.isVisible():
            return
        self.l_gov.set(s.governor or "–")
        self.l_epp.set(EPP_LABELS.get(s.epp, s.epp or "–"))
        self.l_turbo.set("–" if s.no_turbo is None else ("spento" if s.no_turbo else "attivo"),
                         f"P-core ora {s.freq_p_max:.0f} MHz" if s.freq_p_max else "")
        self.l_plat.set(PLATFORM_LABELS.get(s.platform_profile, s.platform_profile or "–"),
                        "rete" if s.ac_online else "batteria")
        li = (self.limits_info or {}).get("current", {})
        if li.get("pl"):
            tp, te = li.get("trl_p"), li.get("trl_e")
            self.l_lim.set(f"{li['pl'][0]}/{li['pl'][1]} W",
                           f"P ×{max(tp)} · E ×{max(te)}" if tp and te else "moltiplicatori non leggibili")
        self.l_freq.set(f"{s.min_freq}–{s.max_freq}" if s.min_freq else "–",
                        f"hardware {s.hw_min}–{s.hw_max_p} MHz")
