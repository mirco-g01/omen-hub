"""Test: guided hardware and power tests with a report an AI can read.

The tests themselves live in omen_hub.testsuite as generators of steps; this
page only runs the steps (instruction, wait for an action, countdown,
background job), animates them and shows the result.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QPushButton, QStackedWidget,
                             QVBoxLayout, QWidget)

from ... import testkit, testsuite
from ...testsuite import Measure, Say, WaitFor, Work
from .. import widgets as W
from ..test_art import ActionArt
from . import Page

STATUS_ICON = {"todo": "○", "run": "▶", "ok": "✓", "warn": "▲", "fail": "✕", "skip": "–"}


class TestsPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Test",
                         "Prove guidate di ventole, CPU, GPU, consumi e standby. Alla fine resta un "
                         "report Markdown in ~/omen-hub-reports da far leggere a un'AI.")
        b = QPushButton("Cartella report")
        b.clicked.connect(lambda: self._open(testkit.REPORT_DIR))
        self.head_actions.addWidget(b)

        self.stack = QStackedWidget()
        self.add(self.stack, 1)
        self.stack.addWidget(self._build_catalog())
        self.stack.addWidget(self._build_run())
        self.stack.addWidget(self._build_result())

        # runner state
        self.gen = None
        self.queue: list[testsuite.TestDef] = []
        self.run_defs: list[testsuite.TestDef] = []
        self.report: testkit.Report | None = None
        self.probes: testkit.Probes | None = None
        self.step = None
        self.step_t0 = 0.0
        self.samples: list[dict] = []
        self.next_probe = 0.0
        self.busy_job = False
        self.tick = QTimer(self, interval=250, timeout=self._on_tick)
        ctx.snapshot.connect(self._on_snapshot)

    # -- catalog ----------------------------------------------------------------------
    def _build_catalog(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        full = W.Card()
        row = QHBoxLayout()
        row.setSpacing(18)
        art = ActionArt(130)
        art.set_scene("measure", animate=False)
        art.phase = 0.6
        row.addWidget(art)
        col = QVBoxLayout()
        col.addWidget(W.label("Test completo", "cardTitle", size=13))
        col.addWidget(W.label("Tutti i test in sequenza, guidati passo passo: consumi a riposo, GPU, "
                              "ventole, CPU, schermo spento e standby. Circa 15–20 minuti; ti dirà lui "
                              "quando staccare caricatore e HDMI e quando non toccare niente.",
                              "cardSub", wrap=True))
        col.addSpacing(6)
        go = QPushButton("Avvia tutti i test", objectName="primary")
        go.setCursor(Qt.CursorShape.PointingHandCursor)
        go.clicked.connect(lambda: self.start(list(testsuite.CATALOG), "Test completo"))
        col.addLayout(W.hrow(go, None))
        row.addLayout(col, 1)
        full.add(row)
        lay.addWidget(full)

        grid = QGridLayout()
        grid.setSpacing(16)
        for i, td in enumerate(testsuite.CATALOG):
            c = W.Card()
            top = QHBoxLayout()
            a = ActionArt(92)
            a.set_scene(td.art, animate=False)
            a.phase = 0.7
            top.addWidget(a)
            tcol = QVBoxLayout()
            tcol.setSpacing(2)
            tcol.addWidget(W.label(td.title, "cardTitle"))
            tcol.addWidget(W.label(td.minutes, "faint"))
            tcol.addStretch(1)
            top.addLayout(tcol, 1)
            c.add(top)
            c.add(W.label(td.description, "cardSub", wrap=True))
            b = QPushButton("Avvia")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, t=td: self.start([t], t.title))
            c.add(W.hrow(None, b))
            grid.addWidget(c, i // 3, i % 3)
        lay.addLayout(grid)

        self.recent = W.Card("Report recenti", "Il più recente è anche in ~/omen-hub-reports/ultimo-report.md.")
        self.recent_box = QVBoxLayout()
        self.recent.add(self.recent_box)
        lay.addWidget(self.recent)
        lay.addStretch(1)
        self._fill_recent()
        return w

    def _fill_recent(self):
        while self.recent_box.count():
            it = self.recent_box.takeAt(0)
            if it.layout():
                while it.layout().count():
                    x = it.layout().takeAt(0)
                    if x.widget():
                        x.widget().deleteLater()
            elif it.widget():
                it.widget().deleteLater()
        files = sorted(testkit.REPORT_DIR.glob("omen-test-*.md"), reverse=True)[:6]
        if not files:
            self.recent_box.addWidget(W.label("Nessun report ancora.", "faint"))
            return
        for f in files:
            b = QPushButton("Apri", objectName="ghost")
            b.clicked.connect(lambda _=False, p=f: self._open(p))
            self.recent_box.addLayout(W.hrow(W.label(f.name), None, b))

    # -- run view ------------------------------------------------------------------------
    def _build_run(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)

        self.chips_card = W.Card()
        self.chips = QHBoxLayout()
        self.chips.setSpacing(18)
        self.chips_card.add(self.chips)
        lay.addWidget(self.chips_card)

        main = W.Card()
        row = QHBoxLayout()
        row.setSpacing(28)
        self.art = ActionArt(300)
        row.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(8)
        self.r_where = W.label("", "faint")
        self.r_title = W.label("", "pageTitle", wrap=True)
        self.r_text = W.label("", wrap=True, size=11.5)
        self.r_text.setMinimumHeight(70)
        self.r_hint = W.label("", "muted", wrap=True)
        self.b_primary = QPushButton("Continua", objectName="primary")
        self.b_skip = QPushButton("Salta")
        self.b_stop = QPushButton("Interrompi", objectName="danger")
        for b in (self.b_primary, self.b_skip, self.b_stop):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_primary.clicked.connect(lambda: self._answer(True))
        self.b_skip.clicked.connect(lambda: self._answer(False))
        self.b_stop.clicked.connect(self.cancel)
        for x in (self.r_where, self.r_title, self.r_text, self.r_hint):
            col.addWidget(x)
        col.addLayout(W.hrow(self.b_primary, self.b_skip, None, self.b_stop))
        col.addSpacing(10)
        tiles = QHBoxLayout()
        tiles.setSpacing(26)
        self.t_temp = W.StatTile("Temperatura", "°C", 18)
        self.t_fans = W.StatTile("Ventole", "RPM", 18)
        self.t_pw = W.StatTile("Potenza", "W", 18)
        self.t_gpu = W.StatTile("GPU", "", 14)
        for t in (self.t_temp, self.t_fans, self.t_pw, self.t_gpu):
            tiles.addWidget(t)
        tiles.addStretch(1)
        col.addLayout(tiles)
        col.addStretch(1)
        row.addLayout(col, 1)
        main.add(row)
        lay.addWidget(main)

        charts = QHBoxLayout()
        charts.setSpacing(16)
        self.c_temp = W.LineChart(["Controllo", "Package"], "°C", window_s=180, y_range=(30, 100), height=130)
        self.c_fan = W.LineChart(["CPU", "GPU"], "RPM", window_s=180, y_range=(0, 3000), height=130)
        for t, ch in (("Temperatura CPU", self.c_temp), ("Ventole", self.c_fan)):
            c = W.Card(t)
            c.add(ch)
            charts.addWidget(c)
        lay.addLayout(charts)
        lay.addStretch(1)
        return w

    # -- result view ---------------------------------------------------------------------
    def _build_result(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        card = W.Card()
        row = QHBoxLayout()
        row.setSpacing(28)
        self.res_art = ActionArt(200)
        row.addWidget(self.res_art, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(8)
        self.res_title = W.label("", "pageTitle")
        self.res_counts = W.label("", "muted")
        self.res_list = QVBoxLayout()
        self.res_list.setSpacing(6)
        self.res_path = W.label("", "faint", wrap=True)
        self.res_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        b_open = QPushButton("Apri report", objectName="primary")
        b_copy = QPushButton("Copia percorso")
        b_dir = QPushButton("Apri cartella")
        b_back = QPushButton("Torna ai test")
        b_open.clicked.connect(lambda: self._open(self.last_path))
        b_copy.clicked.connect(self._copy_path)
        b_dir.clicked.connect(lambda: self._open(testkit.REPORT_DIR))
        b_back.clicked.connect(self._back)
        col.addWidget(self.res_title)
        col.addWidget(self.res_counts)
        col.addSpacing(6)
        col.addLayout(self.res_list)
        col.addSpacing(10)
        col.addWidget(self.res_path)
        col.addLayout(W.hrow(b_open, b_copy, b_dir, None, b_back))
        col.addStretch(1)
        row.addLayout(col, 1)
        card.add(row)
        lay.addWidget(card)
        lay.addStretch(1)
        self.last_path: Path | None = None
        return w

    # -- runner ---------------------------------------------------------------------------
    def start(self, defs: list, title: str):
        if self.gen is not None:
            return
        need_hub = [d.title for d in defs if d.needs_hub]
        if need_hub and not self.ctx.hub_ok:
            self.ctx.toast("omen-hubd non attivo: " + ", ".join(need_hub) + " verranno saltati.", "warn")
        self.run_defs = list(defs)
        self.queue = list(defs)
        self.report = testkit.Report(title)
        self.probes = testkit.Probes()
        self.status = {d.key: "todo" for d in defs}
        self._build_chips()
        self.c_temp.clear()
        self.c_fan.clear()
        self.stack.setCurrentIndex(1)
        self.tick.start()
        self._next_test()

    def _build_chips(self):
        while self.chips.count():
            it = self.chips.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.chip_labels = {}
        for d in self.run_defs:
            lab = W.label("", bold=True)
            self.chip_labels[d.key] = lab
            self.chips.addWidget(lab)
        self.chips.addStretch(1)
        self._paint_chips()

    def _paint_chips(self):
        T = W.T
        col = {"todo": T.faint, "run": T.accent, "ok": T.ok, "warn": T.warn, "fail": T.err, "skip": T.faint}
        for d in self.run_defs:
            st = self.status.get(d.key, "todo")
            lab = self.chip_labels[d.key]
            lab.setText(f"{STATUS_ICON[st]}  {d.title}")
            lab.setStyleSheet(f"color: {col[st].name()};")

    def _next_test(self):
        if not self.queue:
            self._finish(False)
            return
        self.cur_def = self.queue.pop(0)
        self.status[self.cur_def.key] = "run"
        self._paint_chips()
        n = len(self.run_defs) - len(self.queue)
        self.where = f"Test {n} di {len(self.run_defs)} · {self.cur_def.title}"
        self.gen = self.cur_def.fn(self.report, self.probes)
        self._send(None)

    def _send(self, value=None, exc: Exception | None = None):
        try:
            step = self.gen.throw(exc) if exc else self.gen.send(value)
        except StopIteration:
            self._test_done()
            return
        except Exception as e:          # a bug in a test must not kill the run
            import traceback
            traceback.print_exc()
            if self.report.cur is None or self.report.cur.key != self.cur_def.key:
                self.report.section(self.cur_def.key, self.cur_def.title)
            self.report.verdict("fail", f"errore interno del test: {e}")
            self._test_done()
            return
        self._show(step)

    def _test_done(self):
        sec = next((s for s in self.report.sections if s.key == self.cur_def.key), None)
        kinds = [k for k, _ in (sec.verdicts if sec else [])]
        self.status[self.cur_def.key] = ("fail" if "fail" in kinds else "warn" if "warn" in kinds
                                         else "skip" if kinds and set(kinds) <= {"skip"} else "ok")
        self._paint_chips()
        self.gen = None
        self._next_test()

    def _show(self, step):
        self.step = step
        self.step_t0 = time.monotonic()
        self.samples = []
        self.next_probe = 0.0
        self.r_where.setText(self.where)
        self.r_title.setText(step.title)
        self.r_text.setText(step.text)
        self.r_hint.setText("")
        self.b_primary.setVisible(False)
        self.b_skip.setVisible(False)
        self.art.set_progress(None)
        if isinstance(step, Say):
            self.art.set_scene(step.art, animate=step.animate)
            self.b_primary.setText(step.button)
            self.b_primary.setVisible(True)
            self.b_skip.setText(step.skip or "")
            self.b_skip.setVisible(bool(step.skip))
        elif isinstance(step, WaitFor):
            self.art.set_scene(step.art, animate=True)
            self.r_hint.setText("Lo rilevo da solo appena lo fai.")
            if step.skip:
                self.b_skip.setText(step.skip)
                self.b_skip.setVisible(True)
        elif isinstance(step, Measure):
            self.art.set_scene(step.art, animate=step.animate, speed=step.speed)
            if not step.animate:
                self.art.phase = 0.6
                self.art.update()
            self.r_hint.setText("" if step.animate else "Animazioni ferme durante la misura: anche loro consumano.")
            if step.skip:
                self.b_skip.setText(step.skip)
                self.b_skip.setVisible(True)
        elif isinstance(step, Work):
            self.art.set_scene(step.art, animate=True, speed=step.speed)
            self.art.set_progress(None, "…")
            self.busy_job = True
            W.run_async(step.fn, self._job_done, self._job_failed)

    def _job_done(self, res):
        self.busy_job = False
        if self.gen is not None:
            self._send(res)

    def _job_failed(self, msg):
        self.busy_job = False
        if self.gen is not None:
            self._send(exc=RuntimeError(msg))

    def _answer(self, yes: bool):
        st = self.step
        if isinstance(st, Say):
            self._send(yes)
        elif isinstance(st, WaitFor):
            self._send(False)
        elif isinstance(st, Measure):
            self._send(self.samples)

    def _on_tick(self):
        st = self.step
        if st is None or self.gen is None:
            return
        el = time.monotonic() - self.step_t0
        if isinstance(st, WaitFor):
            if el >= self.next_probe:         # once a second
                self.next_probe += 1.0
                try:
                    hit = st.cond()
                except Exception:
                    hit = False
                if hit:
                    self.art.set_scene("done")
                    self.step = None
                    QTimer.singleShot(700, lambda: self._send(True))
                elif el > st.timeout:
                    self._send(False)
        elif isinstance(st, Measure):
            if el >= self.next_probe:
                self.next_probe += st.every
                try:
                    s = st.probe()
                except Exception as exc:
                    s = {"t": time.time(), "error": str(exc)}
                self.samples.append(s)
                self._live(s)
                if st.until and len(self.samples) > 1:
                    try:
                        if st.until(s):
                            self._send(self.samples)
                            return
                    except Exception:
                        pass
            left = max(0, st.seconds - el)
            self.art.set_progress(el / st.seconds, f"{int(left + 0.99)} s")
            if el >= st.seconds:
                self._send(self.samples)

    def _live(self, s: dict):
        self.t_temp.set(s.get("ctrl"), f"pkg {s['pkg']:.0f}°" if s.get("pkg") else "")
        f1, f2 = s.get("fan1"), s.get("fan2")
        self.t_fans.set(f"{f1 or 0}/{f2 or 0}" if f1 is not None else None)
        bw, cw = s.get("bat_w"), s.get("cpu_w")
        if bw is not None:
            self.t_pw.set(bw, "batteria (totale)" + (f" · CPU {cw:.1f}" if cw is not None else ""), fmt="{:.1f}")
        elif cw is not None:
            self.t_pw.set(cw, "CPU package (RAPL)", fmt="{:.1f}")
        g = {"active": "accesa", "suspended": "spenta", "suspending": "si spegne", "resuming": "si accende"}
        self.t_gpu.set(g.get(s.get("gpu"), s.get("gpu") or "–"),
                       "HDMI collegato" if s.get("hdmi") else "")
        self.c_temp.push(s["t"], [s.get("ctrl"), s.get("pkg")])
        self.c_fan.push(s["t"], [f1, f2])

    def _on_snapshot(self, snap):
        # between measurements the app's own 2 s sample keeps the tiles alive
        if self.stack.currentIndex() != 1 or isinstance(self.step, Measure):
            return
        self._live({"t": snap.t, "ctrl": snap.control_temp, "pkg": snap.pkg_temp, "fan1": snap.fan1,
                    "fan2": snap.fan2, "gpu": snap.gpu_state,
                    "bat_w": (snap.battery or {}).get("watts") if snap.ac_online is False else None,
                    "hdmi": self.probes.hdmi_connected() if self.probes else None})

    def cancel(self):
        if self.gen is None:
            return
        if not self.ctx.confirm("Interrompere?", "Interrompo il test? Quello che ho già misurato "
                                                 "finisce comunque nel report."):
            return
        gen, self.gen = self.gen, None
        try:
            gen.close()                 # runs the tests' finally: fans, load...
        except Exception:
            pass
        self.status[self.cur_def.key] = "skip"
        self._finish(True)

    def _finish(self, cancelled: bool):
        self.tick.stop()
        self.step = None
        self.art.freeze()
        try:
            self.last_path = self.report.save(cancelled)
        except OSError as exc:
            self.last_path = None
            self.ctx.toast(f"report non salvato: {exc}", "err")
        c = self.report.counts()
        kind = "fail" if c["fail"] else "warn" if c["warn"] or cancelled else "done"
        self.res_art.set_scene(kind)
        QTimer.singleShot(2500, self.res_art.freeze)
        self.res_title.setText("Test interrotto" if cancelled else "Test completati")
        self.res_counts.setText(f"✓ {c['ok']} ok   ▲ {c['warn']} da guardare   ✕ {c['fail']} problemi")
        while self.res_list.count():
            it = self.res_list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        T = W.T
        colors = {"ok": T.ok, "warn": T.warn, "fail": T.err, "info": T.accent, "skip": T.faint}
        for s in self.report.sections:
            for k, txt in s.verdicts:
                lab = W.label(f"{STATUS_ICON.get(k, '•') if k != 'info' else 'ℹ'}  <b>{s.title}:</b> {txt}", wrap=True)
                lab.setStyleSheet(f"color: {colors.get(k, T.text).name()};")
                self.res_list.addWidget(lab)
        self.res_path.setText(f"Report: {self.last_path}" if self.last_path else "")
        self.stack.setCurrentIndex(2)
        self._fill_recent()

    def _back(self):
        self.res_art.freeze()
        self.stack.setCurrentIndex(0)

    def _copy_path(self):
        if self.last_path:
            QGuiApplication.clipboard().setText(str(self.last_path))
            self.ctx.toast("Percorso del report copiato.", "ok")

    def _open(self, path):
        if not path:
            return
        Path(path).mkdir(parents=True, exist_ok=True) if path == testkit.REPORT_DIR else None
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def hideEvent(self, e):
        # never animate off-screen; a running test keeps its timer (it may
        # have locked the screen on purpose)
        if self.gen is None:
            self.res_art.freeze()
        super().hideEvent(e)
