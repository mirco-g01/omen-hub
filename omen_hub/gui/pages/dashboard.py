"""Panoramica: profile quick switch, live gauges and the last five minutes."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from ... import paths, profiles
from .. import widgets as W
from . import Page


def _fmt_hours(h):
    if h is None:
        return ""
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d} min"


class DashboardPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Panoramica", "Stato della macchina in tempo reale.")
        self.store = None

        # profile switch -------------------------------------------------------
        self.prof_card = W.Card("Profilo energetico", "")
        self.prof_seg_box = QHBoxLayout()
        self.prof_card.add(self.prof_seg_box)
        self.prof_seg = None
        self.prof_hint = W.label("", "faint", wrap=True)
        self.prof_card.add(self.prof_hint)
        self.prof_live = W.label("", "muted", wrap=True)
        self.prof_card.add(self.prof_live)
        self.busy = False
        self.add(self.prof_card)

        # gauges ---------------------------------------------------------------
        g = W.Card()
        row = QHBoxLayout()
        row.setSpacing(18)
        self.g_temp = W.RingGauge("CPU", "°C", 30, 100)
        self.g_load = W.RingGauge("Carico", "%", 0, 100)
        self.g_fan1 = W.RingGauge("Ventola CPU", "RPM", 0, 6000)
        self.g_fan2 = W.RingGauge("Ventola GPU", "RPM", 0, 6000)
        for gg in (self.g_temp, self.g_load, self.g_fan1, self.g_fan2):
            row.addWidget(gg)
        row.addSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(12)
        self.t_fp = W.StatTile("P-core", "MHz", 18)
        self.t_fe = W.StatTile("E-core", "MHz", 18)
        self.t_bat = W.StatTile("Batteria", "%", 18)
        self.t_pw = W.StatTile("Potenza", "W", 18)
        self.t_gpu = W.StatTile("GPU RTX 5070 Ti", "", 14)
        self.t_mem = W.StatTile("Memoria", "GiB", 18)
        for i, t in enumerate((self.t_fp, self.t_fe, self.t_mem, self.t_bat, self.t_pw, self.t_gpu)):
            grid.addWidget(t, i // 3, i % 3)
        row.addLayout(grid, 1)
        g.add(row)
        self.add(g)

        # charts ----------------------------------------------------------------
        self.c_temp = W.LineChart(["Controllo ventole", "Package"], "°C", y_range=(30, 100))
        self.c_fan = W.LineChart(["CPU", "GPU"], "RPM", y_range=(0, 3000))
        self.c_load = W.LineChart(["P-core", "E-core"], "%", y_range=(0, 100))
        self.c_pw = W.LineChart(["Batteria"], "W", fmt="{:.1f}")
        charts = QGridLayout()
        charts.setSpacing(16)
        for i, (title, sub, ch) in enumerate((
            ("Temperatura CPU", "Controllo = la temperatura a cui reagisce la curva "
             "(hybrid: i picchi di un singolo core non contano).", self.c_temp),
            ("Ventole", "Giri al minuto letti dal driver hp-wmi.", self.c_fan),
            ("Carico CPU", "Media per tipo di core.", self.c_load),
            ("Potenza dalla batteria", "Solo a batteria: sotto rete il dato non è significativo.", self.c_pw),
        )):
            c = W.Card(title, sub)
            c.add(ch)
            charts.addWidget(c, i // 2, i % 2)
        self.add(charts)

        # GPU ------------------------------------------------------------------------
        gc = W.Card("Sensori GPU",
                    "nvidia-smi sveglia la GPU dal D3cold e, se interrogata di continuo, "
                    "le impedisce di tornarci (era costato +10 W a riposo). Lo stato qui sopra "
                    "viene da runtime_status e non la tocca.")
        self.gpu_toggle = W.Toggle()
        self.gpu_toggle.toggled.connect(self._gpu_toggled)
        self.gpu_line = W.label("", "muted", wrap=True)
        gc.add(W.hrow(self.gpu_toggle, W.label("Leggi i sensori quando la GPU è già attiva"), None))
        gc.add(self.gpu_line)
        self.add(gc)
        self.body.addStretch(1)

        ctx.snapshot.connect(self.on_snapshot)
        ctx.hub_changed.connect(lambda ok: self.load_profiles())
        self.load_profiles()

    def _gpu_toggled(self, on):
        self.ctx.query_gpu = on

    # profiles ------------------------------------------------------------------------
    def load_profiles(self):
        if self.ctx.hub_ok:
            self.ctx.hub({"cmd": "profiles.get"}, lambda r: self._set_store(r["store"]))
        else:
            self._set_store(profiles.load(paths.PROFILES, paths.AUTOCPUFREQ_CONF))

    def activated(self):
        self.load_profiles()

    def _source(self):
        s = self.ctx.last
        return "ac" if (s is None or s.ac_online is not False) else "battery"

    def _set_store(self, store):
        self.store = store
        if self.prof_seg is not None:
            # setParent(None) would turn the old selector into a stray
            # top-level window: take it out of the layout and destroy it
            self.prof_seg_box.removeWidget(self.prof_seg)
            self.prof_seg.hide()
            self.prof_seg.deleteLater()
        names = list(store["profiles"])
        self.prof_seg = W.Segmented([(n, n) for n in names])
        self.prof_seg.changed.connect(self._pick)
        self.prof_seg_box.addWidget(self.prof_seg)
        self._refresh_profile_label()

    def _refresh_profile_label(self):
        if not self.store:
            return
        src = self._source()
        cur = self.store["assign"].get(src)
        self.prof_seg.set(cur)
        self.prof_seg.setEnabled(self.ctx.hub_ok and not self.busy)
        where = "sotto rete" if src == "ac" else "a batteria"
        other = self.store["assign"].get("battery" if src == "ac" else "ac")
        self.prof_card.subtitle.setText(
            f"Attivo {where}: {cur}.  Quando {'stacchi' if src == 'ac' else 'attacchi'} il cavo passa a: {other}.")
        desc = self.store["profiles"].get(cur, {}).get("description", "")
        self.prof_hint.setText(desc + ("" if self.ctx.hub_ok else
                                       "   (sola lettura: omen-hubd non è installato)"))

    def _pick(self, name):
        if not self.store:
            return
        src = self._source()
        store = {**self.store, "assign": {**self.store["assign"], src: name}}
        self.busy = True
        self.prof_seg.setEnabled(False)

        def done(r):
            self.busy = False
            self._set_store(store)
            self.ctx.toast(f"Profilo «{name}» · {r.get('output', '')}", "ok")

        def failed(_):
            self.busy = False
            self._refresh_profile_label()
        self.ctx.hub({"cmd": "profiles.save", "store": store}, done, on_fail=failed)

    # live -------------------------------------------------------------------------------
    def on_snapshot(self, s):
        T = W.T
        ct = s.control_temp
        col = T.err if (ct or 0) >= 90 else T.warn if (ct or 0) >= 80 else T.accent
        self.g_temp.set(ct, f"pkg {s.pkg_temp:.0f}°" if s.pkg_temp is not None else "", col)
        self.g_load.set(s.load)
        self.g_fan1.set(s.fan1)
        self.g_fan2.set(s.fan2)
        self.t_fp.set(s.freq_p, f"max {s.freq_p_max:.0f} · {s.load_p or 0:.0f} %" if s.freq_p_max else "")
        self.t_fe.set(s.freq_e, f"{s.load_e or 0:.0f} %")
        if s.mem_total:
            used = (s.mem_total - (s.mem_avail or 0)) / 2**30
            swap = ((s.swap_total or 0) - (s.swap_free or 0)) / 2**30
            self.t_mem.set(used, f"di {s.mem_total / 2**30:.0f} · swap {swap:.1f}", fmt="{:.1f}")
        b = s.battery or {}
        status = {"Charging": "in carica", "Discharging": "in uso", "Full": "carica",
                  "Not charging": "non in carica"}.get(b.get("status"), b.get("status") or "")
        hl = _fmt_hours(b.get("hours_left"))
        self.t_bat.set(b.get("capacity"), status + (f" · {hl}" if hl else ""))
        src = "rete" if s.ac_online else "batteria"
        self.t_pw.set(b.get("watts") if s.ac_online is False or (b.get("watts") or 0) > 0.05 else None,
                      f"alimentazione: {src}", fmt="{:.1f}")
        state = {"active": "attiva (D0)", "suspended": "sospesa (D3cold)"}.get(s.gpu_state, s.gpu_state or "–")
        self.t_gpu.set(state, "nvidia " + (s.gpu_path or "").rsplit("/", 1)[-1])
        if s.gpu:
            g = s.gpu
            self.gpu_line.setText(f"{g['temp']:.0f} °C · {g['power']:.0f} W (limite {g['power_limit'] or 0:.0f}) · "
                                  f"{g['util']:.0f} % · {g['clock']:.0f} MHz · VRAM {g['mem_used']:.0f}/"
                                  f"{g['mem_total']:.0f} MiB · {g['pstate']}")
        elif self.ctx.query_gpu:
            self.gpu_line.setText("GPU sospesa: non viene interrogata." if s.gpu_state != "active"
                                  else "nvidia-smi non ha risposto.")
        else:
            self.gpu_line.setText("")

        turbo = "–" if s.no_turbo is None else ("spento" if s.no_turbo else "attivo")
        self.prof_live.setText(f"In vigore adesso: governor {s.governor} · EPP {s.epp} · turbo {turbo} · "
                               f"piattaforma {s.platform_profile} · P-core fino a {s.freq_p_max or 0:.0f} MHz · "
                               f"ventole {s.fan1}/{s.fan2} RPM")
        self.c_temp.push(s.t, [s.control_temp, s.pkg_temp])
        self.c_fan.push(s.t, [s.fan1, s.fan2])
        self.c_load.push(s.t, [s.load_p, s.load_e])
        self.c_pw.push(s.t, [b.get("watts") if s.ac_online is False else None])
        if self.store:
            self._refresh_profile_label()
