"""Servizi: every component with its status, start/stop/enable, install/remove,
logs, plus the health checks that encode months of fixes."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QMenu, QPlainTextEdit,
                             QPushButton, QVBoxLayout, QWidget, QComboBox)

from ... import components, desktop, group
from .. import widgets as W
from . import Page


class LogDialog(QDialog):
    def __init__(self, parent, comp: components.Component):
        super().__init__(parent)
        self.setWindowTitle(f"Log · {comp.name}")
        self.resize(980, 620)
        lay = QVBoxLayout(self)
        self.units = list(comp.units)
        self.cb = QComboBox()
        for u in self.units:
            self.cb.addItem(f"{u.name} ({'utente' if u.scope == 'user' else 'sistema'})")
        self.cb.currentIndexChanged.connect(self.load)
        btn = QPushButton("Aggiorna")
        btn.clicked.connect(self.load)
        lay.addLayout(W.hrow(self.cb, None, btn))
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.text, 1)
        self.load()

    def load(self, *_):
        u = self.units[self.cb.currentIndex()]
        self.text.setPlainText("caricamento…")
        W.run_async(lambda: desktop.journal(u.name, u.scope), self._show)

    def _show(self, txt):
        self.text.setPlainText(txt)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


class OutputDialog(QDialog):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 480)
        lay = QVBoxLayout(self)
        t = QPlainTextEdit(readOnly=True)
        t.setPlainText(text or "(nessun output)")
        lay.addWidget(t)
        b = QPushButton("Chiudi")
        b.clicked.connect(self.accept)
        lay.addLayout(W.hrow(None, b))


class ComponentRow(QWidget):
    def __init__(self, page, comp: components.Component):
        super().__init__()
        self.page, self.c = page, comp
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.setSpacing(14)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.name = W.label(comp.name, bold=True)
        self.desc = W.label(comp.description, "muted", wrap=True)
        self.units = W.label("", "faint", wrap=True)
        col.addWidget(self.name)
        col.addWidget(self.desc)
        col.addWidget(self.units)
        lay.addLayout(col, 1)
        self.pill = W.Pill()
        self.pill.setFixedWidth(130)
        lay.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignTop)
        self.btn = QPushButton("Azioni ▾")
        self.btn.setFixedWidth(110)
        self.menu = QMenu(self)
        self.btn.setMenu(self.menu)
        lay.addWidget(self.btn, 0, Qt.AlignmentFlag.AlignTop)
        self.status = {}

    def update_status(self, st: dict):
        self.status = st
        summ = st.get("summary", "missing")
        hook = st.get("hook")
        txt = None
        if self.c.hook:
            txt = {"installed": "● installato", "disabled": "○ rimosso", "inactive": "○ non eseguibile",
                   "missing": "– assente"}.get(hook)
        if not txt and summ == "ok" and self.c.units and all(u.oneshot for u in self.c.units):
            txt = "● abilitato"
        self.pill.set(summ, txt)
        bits = []
        if self.c.hook:
            bits.append(f"/usr/lib/systemd/system-sleep/{self.c.hook}")
        for u in st.get("units", []):
            scope = "utente" if u["scope"] == "user" else "sistema"
            if u["load"] != "loaded":
                bits.append(f"{u['name']} ({scope}): non installato")
            else:
                bits.append(f"{u['name']} ({scope}): {u['active']}/{u['sub']}, {u['enabled'] or '–'}")
        if self.c.source:
            bits.append(f"origine: {self.c.source}")
        if self.c.notes:
            bits.append(self.c.notes)
        self.units.setText("\n".join(bits))
        self._build_menu()

    def _build_menu(self):
        m = self.menu
        m.clear()
        hub = self.page.ctx.hub_ok
        for u in self.status.get("units", []):
            if u["load"] != "loaded":
                continue
            sub = m.addMenu(u["name"])
            needs_hub = u["scope"] == "system"
            for action, label in (("start", "Avvia"), ("stop", "Ferma"), ("restart", "Riavvia"),
                                  ("enable", "Abilita all'avvio (e avvia)"),
                                  ("disable", "Disabilita all'avvio (e ferma)")):
                a = sub.addAction(label)
                a.setEnabled(hub or not needs_hub)
                a.triggered.connect(lambda _=False, act=action, unit=u: self.page.unit_action(self.c, unit, act))
        if self.c.hook and self.c.removable_hook:
            m.addSeparator()
            hs = self.status.get("hook")
            if hs == "installed":
                a = m.addAction("Rimuovi hook (lo parcheggia in /var/lib/omen-hub)")
                a.triggered.connect(lambda: self.page.component_action(self.c, "uninstall"))
            else:
                a = m.addAction("Installa hook")
                a.triggered.connect(lambda: self.page.component_action(self.c, "install"))
            a.setEnabled(hub)
        if self.c.install or self.c.uninstall:
            m.addSeparator()
            if self.c.install:
                a = m.addAction("Installa / reinstalla dal progetto")
                a.setEnabled(hub)
                a.triggered.connect(lambda: self.page.component_action(self.c, "install"))
            if self.c.uninstall:
                a = m.addAction("Disinstalla")
                a.setEnabled(hub)
                a.triggered.connect(lambda: self.page.component_action(self.c, "uninstall"))
        if self.c.units:
            m.addSeparator()
            a = m.addAction("Mostra log…")
            a.triggered.connect(lambda: LogDialog(self, self.c).exec())
        self.btn.setEnabled(not m.isEmpty())


class ServicesPage(Page):
    def __init__(self, ctx):
        super().__init__(ctx, "Servizi",
                         "Tutto quello che è stato installato su questo portatile, in un posto solo.")
        self.needs_hub_banner()
        self.btn_refresh = QPushButton("Aggiorna")
        self.btn_refresh.clicked.connect(self.activated)
        self.head_actions.addWidget(self.btn_refresh)

        self.group = W.Card("Gruppo OMEN",
                            "Tutti i servizi hardware come un blocco unico: omen-hub.target (sistema) "
                            "e omen-hub-user.target (sessione). «Avvia» accende solo quelli abilitati; "
                            "omen-hubd, gli hook di sospensione e gli extra del desktop restano fuori.")
        self.group_pill = W.Pill()
        self.group.header.addWidget(self.group_pill, 0, Qt.AlignmentFlag.AlignTop)
        self.group_units = W.label("", "faint", wrap=True)
        self.group.add(self.group_units)
        self.group_btns = {}
        for action, text in (("start", "▶  Avvia tutto"), ("restart", "↻  Riavvia tutto"),
                             ("stop", "■  Ferma tutto")):
            b = QPushButton(text)
            if action == "stop":
                b.setObjectName("danger")
            b.clicked.connect(lambda _=False, a=action: self.group_action(a))
            self.group_btns[action] = b
        self.group.add(W.hrow(self.group_btns["start"], self.group_btns["restart"], None,
                              self.group_btns["stop"]))
        self.add(self.group)

        self.health = W.Card("Controlli di integrità",
                             "Le condizioni da cui dipendono i fix fatti finora.")
        self.health_grid = QGridLayout()
        self.health_grid.setHorizontalSpacing(12)
        self.health_grid.setVerticalSpacing(8)
        self.health.add(self.health_grid)
        self.add(self.health)

        self.rows: dict[str, ComponentRow] = {}
        for group in components.GROUPS:
            comps = [c for c in components.COMPONENTS if c.group == group]
            if not comps:
                continue
            card = W.Card(group)
            for i, c in enumerate(comps):
                if i:
                    sep = QWidget()
                    sep.setFixedHeight(1)
                    sep.setStyleSheet(f"background: {W.T.border.name()};")
                    card.add(sep)
                row = ComponentRow(self, c)
                self.rows[c.id] = row
                card.add(row)
            self.add(card)
        self.body.addStretch(1)

    def activated(self):
        W.run_async(lambda: (components.statuses(), components.health_checks()), self._show)
        self.refresh_group()

    # the OMEN group ---------------------------------------------------------------------
    def refresh_group(self):
        W.run_async(lambda: (group.status("system"), group.status("user")), self._show_group,
                    lambda m: self.group_units.setText(m))

    def _show_group(self, res):
        lines, up, total, installed = [], 0, 0, True
        for st in res:
            installed &= st["target_load"] == "loaded"
            names = []
            for u in st["units"]:
                if u["load"] != "loaded":
                    continue
                total += 1
                on = u["active"] == "active"
                up += on
                names.append(("● " if on else "○ ") + u["name"].removesuffix(".service")
                             + ("" if u["enabled"] in ("enabled", "static") else " (disabilitato)"))
            scope = "sistema" if st["scope"] == "system" else "sessione"
            lines.append(f"{st['target']} ({scope}, {st['target_active']}): " + " · ".join(names))
        self.group_units.setText("\n".join(lines))
        if not installed:
            self.group_pill.set("missing", "– target non installati")
        else:
            self.group_pill.set("ok" if up == total else "warn" if up else "off",
                                f"● {up}/{total} attivi" if up else f"○ 0/{total} attivi")
        for a, b in self.group_btns.items():
            b.setEnabled(installed and (self.ctx.hub_ok or False))

    def group_action(self, action):
        if action == "stop" and not self.ctx.confirm(
                "Ferma tutto",
                "Fermare tutti i servizi OMEN? Le ventole tornano al controllo del BIOS, "
                "auto-cpufreq smette di applicare i profili, tastiera e barra tornano al firmware.\n\n"
                "omen-hubd resta attivo: puoi riavviarli da qui."):
            return
        verb = {"start": "avviati", "stop": "fermati", "restart": "riavviati"}[action]
        self.ctx.toast("Gruppo OMEN: in corso…", "warn")

        def user_half(_reply):
            W.run_async(lambda: group.act("user", action),
                        lambda _: (self.ctx.toast(f"Servizi OMEN {verb}", "ok"), self.activated()),
                        lambda m: (self.ctx.toast(m, "err"), self.activated()))
        # System first: a restarted omen-fxd is what the session's notifier talks to.
        self.ctx.hub({"cmd": "group", "action": action}, user_half, timeout=180,
                     on_fail=lambda _: self.activated())

    def hub_state(self, ok):
        self.refresh_group()
        for r in self.rows.values():
            if r.status:
                r._build_menu()

    def _show(self, res):
        sts, checks = res
        for cid, row in self.rows.items():
            row.update_status(sts.get(cid, {}))
        while self.health_grid.count():
            w = self.health_grid.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        for r, ch in enumerate(checks):
            icon, col = (("✓", W.T.ok) if ch.ok else ("ⓘ", W.T.muted) if ch.ok is None else ("✕", W.T.err))
            ic = W.label(icon, bold=True)
            ic.setStyleSheet(f"color: {col.name()};")
            self.health_grid.addWidget(ic, r, 0, Qt.AlignmentFlag.AlignTop)
            self.health_grid.addWidget(W.label(ch.title), r, 1, Qt.AlignmentFlag.AlignTop)
            det = W.label(ch.detail + (f"\n{ch.hint}" if ch.hint else ""), "muted", wrap=True)
            self.health_grid.addWidget(det, r, 2)
        self.health_grid.setColumnStretch(2, 1)
        bad = sum(1 for c in checks if c.ok is False)
        self.health.subtitle.setText("Tutto in ordine." if not bad else
                                     f"{bad} controll{'o' if bad == 1 else 'i'} da guardare.")

    # actions ------------------------------------------------------------------------------
    def unit_action(self, comp, unit, action):
        verbs = {"start": "avviato", "stop": "fermato", "restart": "riavviato",
                 "enable": "abilitato", "disable": "disabilitato"}
        msg = f"{unit['name']} {verbs[action]}"
        if unit["scope"] == "user":
            W.run_async(lambda: desktop.user_unit(action, unit["name"]),
                        lambda _: (self.ctx.toast(msg, "ok"), self.activated()),
                        lambda m: (self.ctx.toast(m, "err"), self.activated()))
        else:
            self.ctx.hub({"cmd": "unit", "unit": unit["name"], "action": action},
                         lambda _: self.activated(), success=msg, on_fail=lambda _: self.activated())

    def component_action(self, comp, action):
        what = "installare" if action == "install" else "rimuovere"
        extra = ""
        if comp.hook:
            extra = ("\n\nL'hook viene spostato in /var/lib/omen-hub/disabled-hooks/ (mai lasciato "
                     "in system-sleep: systemd eseguirebbe anche le copie).") if action == "uninstall" else ""
        elif action == "install" and comp.install:
            extra = f"\n\nVerrà eseguito come root:\n{' '.join(comp.install)}"
        elif comp.uninstall:
            extra = f"\n\nVerrà eseguito come root:\n{' '.join(comp.uninstall)}"
        if not self.ctx.confirm(comp.name, f"Vuoi {what} «{comp.name}»?{extra}"):
            return
        self.ctx.toast(f"{comp.name}: in corso…", "warn")

        def done(r):
            self.activated()
            out = r.get("output", "")
            if comp.hook:
                self.ctx.toast(out or "fatto", "ok")
            else:
                OutputDialog(self, f"{comp.name} · {action}", out).exec()

        self.ctx.hub({"cmd": "component", "id": comp.id, "action": action}, done, timeout=960,
                     on_fail=lambda _: self.activated())
