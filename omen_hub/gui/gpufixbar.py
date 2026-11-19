"""The bar above every page for the NVIDIA RTD3 fix (omen_hub/gpufix.py).

Hidden while RTD3 works. Otherwise it says what is wrong and offers the one
action that fixes it: «Applica correzione» (through omen-hubd, as root) or
«Riavvia ora» (Plasma's own reboot prompt). While omen-hubd works it shows
the step it is on; «Dettagli» shows the commands and their output.

The state is computed here, as the user (no root, ~20 ms, never wakes the
GPU), on start, when the window comes back, every 5 minutes and after a job.
"""

from __future__ import annotations

import subprocess

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout

from .. import gpufix, protocol
from . import widgets as W

REFRESH_MS = 5 * 60 * 1000
POLL_MS = 2000


class LogDialog(QDialog):
    def __init__(self, parent, text):
        super().__init__(parent)
        self.setWindowTitle("Correzione RTD3 NVIDIA · dettagli")
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        t = QPlainTextEdit(readOnly=True)
        t.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        t.setPlainText(text or "(nessun output)")
        t.verticalScrollBar().setValue(t.verticalScrollBar().maximum())
        lay.addWidget(t)
        b = QPushButton("Chiudi")
        b.clicked.connect(self.accept)
        lay.addLayout(W.hrow(None, b))


class GpuFixBar(QFrame):
    def __init__(self, ctx):
        super().__init__(objectName="banner")
        self.ctx = ctx
        self.status: gpufix.Status | None = None
        self.job: dict = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 14, 10)
        lay.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title = W.label("", bold=True)
        self.detail = W.label("", "muted", wrap=True)
        col.addWidget(self.title)
        col.addWidget(self.detail)
        lay.addLayout(col, 1)
        self.btn_log = QPushButton("Dettagli")
        self.btn_log.clicked.connect(lambda: LogDialog(self, self.job.get("log", "")).exec())
        self.btn_apply = QPushButton("Applica correzione", objectName="primary")
        self.btn_apply.clicked.connect(self.apply)
        self.btn_reboot = QPushButton("Riavvia ora", objectName="primary")
        self.btn_reboot.clicked.connect(self.reboot)
        for b in (self.btn_log, self.btn_apply, self.btn_reboot):
            lay.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()
        self.timer = QTimer(self, interval=REFRESH_MS, timeout=self.refresh)
        self.poll_timer = QTimer(self, interval=POLL_MS, timeout=self.poll)
        ctx.hub_changed.connect(lambda _ok: self.refresh())

    # lifecycle (MainWindow show/hide) --------------------------------------------------------
    def start(self):
        self.refresh()
        self.timer.start()

    def stop(self):
        self.timer.stop()          # a running job keeps being polled: it is cheap and short-lived

    # state ---------------------------------------------------------------------------------------
    def refresh(self):
        W.run_async(gpufix.status, self._show, lambda _m: None)
        if self.ctx.hub_ok and not self.poll_timer.isActive():
            self.poll()            # a job started before this window, or its failure

    def _show(self, st: gpufix.Status):
        self.status = st
        if st.code == "running" or self.job.get("state") == "running":
            self._running()
            return
        if not st.needs_attention:
            self.hide()
            return
        detail = st.detail
        if self.job.get("state") == "failed" and st.action == "apply":
            detail += f"\nUltimo tentativo non riuscito: {self.job.get('message', '')}"
        if st.action == "apply" and not self.ctx.hub_ok:
            detail += "\nPer applicarla serve omen-hubd: sudo ~/omen-hub/install.sh"
        self.title.setText("▲  " + st.title)
        self.detail.setText(detail)
        self.btn_apply.setVisible(st.action == "apply")
        self.btn_apply.setEnabled(self.ctx.hub_ok)
        self.btn_apply.setText("Riapplica correzione" if st.code in ("lost", "ineffective") else "Applica correzione")
        self.btn_reboot.setVisible(st.action == "reboot")
        self.btn_log.setVisible(bool(self.job.get("log")))
        self.show()

    def _running(self):
        self.title.setText("⟳  Correzione GPU NVIDIA in corso")
        self.detail.setText((self.job.get("step") or "omen-hubd sta lavorando…")
                            + "\nPuoi continuare a usare il portatile; alla fine servirà un riavvio.")
        self.btn_apply.hide()
        self.btn_reboot.hide()
        self.btn_log.setVisible(bool(self.job.get("log")))
        self.show()
        if not self.poll_timer.isActive():
            self.poll_timer.start()

    # the job in omen-hubd -----------------------------------------------------------------------
    def apply(self):
        st = self.status
        if st is None:
            return
        swap = st.facts.get("pkg") == gpufix.PREBUILT
        text = ("omen-hubd eseguirà come root:\n\n"
                + ("• pacman -S nvidia-open-dkms: sostituisce nvidia-open precompilato (serve la rete)\n"
                   if swap else "")
                + "• patch di nv-pci.c nel sorgente DKMS (CachyOS 0004, «Open sysfs config from the "
                  "userspace root on 7.3+»), con copia in /var/lib/omen-hub/backup\n"
                + "• dkms build + install del modulo NVIDIA per ogni kernel installato\n"
                + "• mkinitcpio -P (i moduli nvidia sono nell'initramfs)\n\n"
                + f"Ci vogliono {'5–10' if swap else '3–5'} minuti, durante i quali puoi usare il portatile. "
                  "Alla fine va riavviato.")
        if not self.ctx.confirm("Correzione RTD3 NVIDIA", text):
            return
        self.job = {"state": "running", "step": "Avvio…"}
        self._running()
        self.ctx.hub({"cmd": "gpufix", "action": "apply"}, lambda r: self._job(r.get("job") or {}),
                     timeout=10, on_fail=lambda _m: self._job({"state": "failed", "message": _m}))

    def poll(self):
        W.run_async(lambda: protocol.send({"cmd": "gpufix", "action": "job"}, timeout=5),
                    lambda r: self._job(r.get("job") or {}), lambda _m: self.poll_timer.stop())

    def _job(self, job: dict):
        was = self.job.get("state")
        self.job = job
        state = job.get("state")
        if state == "running":
            self._running()
            return
        self.poll_timer.stop()
        if was == "running" and state == "done":
            self.ctx.toast("Driver NVIDIA corretto: riavvia per spegnere la GPU a riposo.", "ok")
        elif was == "running" and state == "failed":
            self.ctx.toast(f"Correzione GPU non riuscita: {job.get('message', '')}", "err")
        if state != was:           # finished now, or a failure from before this window: redraw
            W.run_async(gpufix.status, self._show, lambda _m: None)

    # reboot -------------------------------------------------------------------------------------
    def reboot(self):
        """Plasma's reboot prompt (it lets you save and cancel); systemctl as a fallback."""
        try:
            from PyQt6.QtDBus import QDBusConnection, QDBusInterface
            iface = QDBusInterface("org.kde.LogoutPrompt", "/LogoutPrompt", "org.kde.LogoutPrompt",
                                   QDBusConnection.sessionBus())
            if iface.isValid() and not iface.call("promptReboot").errorMessage():
                return
        except Exception:
            pass
        if self.ctx.confirm("Riavvia", "Riavviare adesso il portatile?"):
            subprocess.Popen(["systemctl", "reboot"], start_new_session=True)
