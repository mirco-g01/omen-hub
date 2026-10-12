from __future__ import annotations

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .. import widgets as W


class Page(QScrollArea):
    def __init__(self, ctx, title: str, subtitle: str = ""):
        super().__init__()
        self.ctx = ctx
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.setWidget(inner)
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(28, 24, 28, 28)
        self.body.setSpacing(16)
        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(W.label(title, "pageTitle"))
        if subtitle:
            col.addWidget(W.label(subtitle, "pageSub", wrap=True))
        head.addLayout(col, 1)
        self.head_actions = QHBoxLayout()
        head.addLayout(self.head_actions)
        self.body.addLayout(head)
        self.banner = None
        ctx.hub_changed.connect(self._hub_changed)

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.body.addWidget(w, stretch)
        else:
            self.body.addLayout(w, stretch)
        return w

    def needs_hub_banner(self):
        """Shown on pages whose actions go through omen-hubd."""
        self.banner = QFrame(objectName="banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(14, 10, 14, 10)
        lab = W.label("omen-hubd non è installato: la pagina è in sola lettura. "
                      "Per abilitare le modifiche esegui una volta in un terminale: "
                      "<b>sudo ~/omen-hub/install.sh</b>", wrap=True)
        bl.addWidget(lab, 1)
        self.body.insertWidget(1, self.banner)
        self.banner.setVisible(not self.ctx.hub_ok)

    def _hub_changed(self, ok):
        if self.banner is not None:
            self.banner.setVisible(not ok)
        self.hub_state(ok)

    def hub_state(self, ok: bool):
        pass
