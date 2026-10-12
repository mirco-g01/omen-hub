"""OMEN Hub main window."""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import QEvent, QPropertyAnimation, QTimer, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (QApplication, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
                             QMessageBox)

from .. import __version__, paths, protocol, sysinfo
from . import style, widgets as W

SAMPLE_MS = 2000      # same cadence as the fan daemon, so the estimator matches it


class Ctx(QObject):
    """Shared state handed to every page."""
    snapshot = pyqtSignal(object)
    hub_changed = pyqtSignal(bool)
    fan_config_changed = pyqtSignal(dict)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.sampler = sysinfo.Sampler()
        self.last: sysinfo.Snapshot | None = None
        self.hub_ok = False
        self.query_gpu = False

    # requests to omen-hubd, off the GUI thread
    def hub(self, req: dict, on_done=None, timeout=30.0, success: str | None = None, on_fail=None):
        def call():
            return protocol.send(req, timeout=timeout)

        def done(reply):
            if success:
                self.toast(success, "ok")
            if on_done:
                on_done(reply)

        def failed(msg):
            self.toast(msg, "err")
            if on_fail:
                on_fail(msg)
        W.run_async(call, done, failed)

    def toast(self, text: str, kind: str = "ok"):
        self.window.show_toast(text, kind)

    def confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self.window, title, text) == QMessageBox.StandardButton.Yes


class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setMaximumWidth(460)
        self.hide()
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", self)
        self.timer = QTimer(self, singleShot=True, timeout=self._fade)

    def show_msg(self, text, kind):
        c = {"ok": W.T.ok, "err": W.T.err, "warn": W.T.warn}.get(kind, W.T.accent)
        icon = {"ok": "✓", "err": "✕", "warn": "▲"}.get(kind, "•")
        self.setText(f"{icon}  {text}")
        self.setStyleSheet(f"background: {W.T.surface2.name()}; border: 1px solid {c.name()};"
                           f"border-left: 4px solid {c.name()}; border-radius: 10px; padding: 10px 14px;")
        self.adjustSize()
        p = self.parentWidget()
        self.move(p.width() - self.width() - 24, p.height() - self.height() - 24)
        self.anim.stop()
        self.effect.setOpacity(1.0)
        self.show()
        self.raise_()
        self.timer.start(6000 if kind == "err" else 3500)

    def _fade(self):
        self.anim.setDuration(400)
        self.anim.setStartValue(1.0)
        self.anim.setEndValue(0.0)
        self.anim.finished.connect(self.hide)
        self.anim.start()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OMEN Hub")
        self.setWindowIcon(app_icon())
        self.resize(1320, 880)
        self.setMinimumSize(980, 640)
        self.ctx = Ctx(self)

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        side = QWidget(objectName="sidebar")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        sl.addWidget(W.label("OMEN Hub", "brand"))
        sl.addWidget(W.label("HP OMEN MAX 16 · 8D41", "brandSub"))
        sl.addSpacing(18)

        from .pages.dashboard import DashboardPage
        from .pages.performance import PerformancePage
        from .pages.fans import FansPage
        from .pages.lighting import LightingPage
        from .pages.services import ServicesPage
        from .pages.tests import TestsPage
        from .pages.fxstudio import FxStudioPage

        self.stack = QStackedWidget()
        self.pages = []
        self.nav_buttons = []
        self.names = {}
        for name, icon, text, cls in (
            ("dashboard", "speedometer", "Panoramica", DashboardPage),
            ("performance", "preferences-system-power-management", "Prestazioni", PerformancePage),
            ("fans", "weather-windy", "Ventole", FansPage),
            ("lighting", "brightness-high", "Luci e schermo", LightingPage),
            ("services", "preferences-system-services", "Servizi", ServicesPage),
            ("tests", "system-run", "Test", TestsPage),
        ):
            self.names[name] = len(self.pages)
            b = QPushButton(f"  {text}", objectName="nav")
            ic = QIcon.fromTheme(icon)
            if not ic.isNull():
                b.setIcon(ic)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            idx = len(self.pages)
            b.clicked.connect(lambda _=False, i=idx: self.go(i))
            sl.addWidget(b)
            self.nav_buttons.append(b)
            page = cls(self.ctx)
            self.pages.append(page)
            self.stack.addWidget(page)
        # Sub-pages: reached from a page, not from the sidebar, which keeps
        # their parent highlighted (name -> index of the parent page).
        self.parent_of = {}
        for name, parent, cls in (("fxstudio", "lighting", FxStudioPage),):
            self.names[name] = len(self.pages)
            self.parent_of[len(self.pages)] = self.names[parent]
            page = cls(self.ctx)
            self.pages.append(page)
            self.stack.addWidget(page)
        self.current = None
        sl.addStretch(1)
        self.hub_label = W.label("", "faint", wrap=True)
        sl.addWidget(self.hub_label)
        sl.addWidget(W.label(f"v{__version__}", "faint"))

        lay.addWidget(side)
        lay.addWidget(self.stack, 1)

        self.toast = Toast(root)
        self.timer = QTimer(self, interval=SAMPLE_MS, timeout=self.tick)
        self.hub_timer = QTimer(self, interval=15000, timeout=self.check_hub)
        self.check_hub()
        self.tick()
        self.go(0)
        self.timer.start()
        self.hub_timer.start()

    def show_toast(self, text, kind="ok"):
        self.toast.show_msg(text, kind)

    def go(self, i):
        lit = self.parent_of.get(i, i)
        for k, b in enumerate(self.nav_buttons):
            b.setChecked(k == lit)
        if self.current is not None and self.current != i:
            self._leave(self.current)
        self.current = i
        self.stack.setCurrentIndex(i)
        page = self.pages[i]
        if hasattr(page, "activated"):
            page.activated()

    def go_name(self, name):
        self.go(self.names[name])

    def _leave(self, i):
        page = self.pages[i]
        if hasattr(page, "deactivated"):
            page.deactivated()

    def closeEvent(self, e):
        # Live previews hold the lights only while their page is open.
        if self.current is not None:
            self._leave(self.current)
        super().closeEvent(e)

    def check_hub(self):
        ok = protocol.available()
        if ok != self.ctx.hub_ok or not self.hub_label.text():
            self.ctx.hub_ok = ok
            self.hub_label.setText("● omen-hubd attivo" if ok else "○ omen-hubd assente: sola lettura")
            self.hub_label.setStyleSheet(f"color: {(W.T.ok if ok else W.T.warn).name()};")
            self.ctx.hub_changed.emit(ok)

    def tick(self):
        cfg_fan = None
        try:
            from .. import desktop
            cfg_fan = desktop.fan_config()
        except Exception:
            pass
        if cfg_fan:
            self.ctx.sampler.configure_estimator(int(cfg_fan.get("ma_window", 20)),
                                                 int(cfg_fan.get("spike_window", 15)))
        s = self.ctx.sampler.sample(query_gpu=self.ctx.query_gpu)
        self.ctx.last = s
        self.ctx.snapshot.emit(s)

    # Stop sampling while nobody looks: every wakeup costs on this machine.
    def changeEvent(self, e):
        if e.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self.timer.stop()
            elif not self.timer.isActive():
                self.tick()
                self.timer.start()
        super().changeEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        self.hub_timer.stop()
        super().hideEvent(e)

    def showEvent(self, e):
        if not self.timer.isActive():
            self.timer.start()
            self.hub_timer.start()
        super().showEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            p = self.toast.parentWidget()
            self.toast.move(p.width() - self.toast.width() - 24, p.height() - self.toast.height() - 24)


ICON_DIR = paths.PAYLOAD.parent / "share" / "icons"
INSTANCE = f"omen-hub-{os.getuid()}"


def app_icon() -> QIcon:
    """The installed theme icon, or the SVG next to the code when run from the repo."""
    fallback = QIcon()
    for png in sorted(ICON_DIR.glob("omen-hub-*.png")):
        fallback.addFile(str(png))
    if fallback.isNull():
        fallback = QIcon(str(ICON_DIR.parent / "omen-hub.svg"))
    return QIcon.fromTheme("omen-hub", fallback)


def hand_over() -> bool:
    """If a hub is already running, ask it to come forward and return True.

    The OMEN key starts the app every time it is pressed; a second window would
    be useless. The Wayland activation token kglobalaccel gave *this* process is
    passed along, because KWin only lets a window take focus with one.
    """
    from PyQt6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if not sock.waitForConnected(300):
        return False
    sock.write(("show " + os.environ.get("XDG_ACTIVATION_TOKEN", "")).encode())
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def serve_instance(win) -> object:
    from PyQt6.QtNetwork import QLocalServer
    server = QLocalServer(win)
    QLocalServer.removeServer(INSTANCE)       # stale socket from a crash
    server.listen(INSTANCE)

    def incoming():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read():
            msg = bytes(conn.readAll()).decode(errors="replace").split(" ", 1)
            if msg and msg[0] == "show":
                token = msg[1].strip() if len(msg) > 1 else ""
                if token:
                    # Qt's xdg-activation support picks it up on requestActivate.
                    os.environ["XDG_ACTIVATION_TOKEN"] = token
                if win.isMinimized():
                    win.showNormal()
                win.show()
                win.raise_()
                win.activateWindow()
            conn.deleteLater()
        conn.readyRead.connect(read)
    server.newConnection.connect(incoming)
    return server


def main(argv=None):
    app = QApplication(argv or sys.argv)
    app.setApplicationName("omen-hub")
    app.setDesktopFileName("omen-hub")
    if hand_over():
        return 0
    app.setWindowIcon(app_icon())
    W.T = style.tokens(app)
    app.setStyleSheet(style.stylesheet(W.T))
    win = MainWindow()
    win._instance = serve_instance(win)
    win.show()
    page = os.environ.get("OMEN_HUB_PAGE")
    if page:
        win.go(int(page) if page.isdigit() else win.names[page])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
