"""Studio luci: the full omen-fx editor, inside the hub.

Everything omen-fx-gui did about the look of the lights, tab for tab --
themes, zone layouts, the default look of each surface and the alert effects --
with the hub's cards, toggles and toasts instead of a window of its own. The logic
of each panel is the one from omen_fx/gui.py, zonegui.py and themegui.py. Its
fourth tab, the machine-wide switches (brightness keys, idle dim, keyboard
level per power source), lives on the Luci e schermo page instead. The
effects are built by omen-fx's engine itself (see fxengine), and every change
goes through omen-fxd's control socket, which owns /etc/omen-fx.

Live previews hold the hardware only while this page is on screen: leaving it
(or closing the hub) gives both surfaces back to the saved configuration,
just as closing omen-fx-gui did.
"""

from __future__ import annotations

from copy import deepcopy

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QListWidget,
                             QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

from ... import desktop, fxengine as E
from ...protocol import HubError
from .. import widgets as W
from . import Page


class FxStudioPage(Page):
    """The page; the panels below are built only if the omen-fx engine loads."""

    TABS = (("themes", "Temi"), ("zones", "Zone"), ("base", "Predefinito"),
            ("events", "Eventi ed effetti"))

    def __init__(self, ctx):
        super().__init__(ctx, "Studio luci",
                         "Temi, zone, aspetto predefinito ed effetti di tastiera e barra luminosa. "
                         "L'anteprima usa lo stesso motore di omen-fxd: quello che vedi è quello "
                         "che faranno i LED.")
        back = QPushButton("←  Luci e schermo", objectName="ghost")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.clicked.connect(lambda: ctx.window.go_name("lighting"))
        self.head_actions.addWidget(back)
        self.ready = False
        self.effects: dict = {}
        self.triggers: list = []
        self.layouts: dict = {}
        self.fast_write = True
        # Alerts, unlike the default profiles, only reach the daemon on Save.
        # Without a visible mark, an edited value looks live and is not.
        self._dirty = False
        self._loading_effect = False

        if not E.load():
            card = W.Card("Motore omen-fx non disponibile", E.ERROR)
            card.add(W.label("Reinstalla omen-fx (~/omen-rgb-keyboard/contrib/omen-fx/install.sh) "
                             "e riapri l'hub.", "muted", wrap=True))
            self.add(card)
            self.body.addStretch(1)
            return
        from .. import fxwidgets as FW
        self.FW = FW

        bar = QHBoxLayout()
        self.tabs = W.Segmented(list(self.TABS))
        self.tabs.changed.connect(self._tab_changed)
        bar.addWidget(self.tabs)
        bar.addStretch(1)
        self.status = W.label("", "faint")
        bar.addWidget(self.status)
        self.add(bar)

        # Not a QStackedWidget: a stack is as tall as its tallest page, so every
        # short tab would inherit the alert editor's height. Hidden panels in a
        # plain column take no room at all.
        self.stack = QWidget()
        stack_col = QVBoxLayout(self.stack)
        stack_col.setContentsMargins(0, 0, 0, 0)
        self._current_panel = None
        self.theme_panel = ThemePanel(self, self._send, self._capture_theme, self._prepare_theme)
        self.theme_panel.applied.connect(self.reload)
        # A zone's effect is an ordinary effect, so the same editor serves --
        # minus the layout picker, since a zone cannot contain a layout.
        self.layout_panel = LayoutPanel(
            self, self._send,
            lambda: FW.EffectEditor(base_mode=True, allow_layout=False, zone_mode=True))
        self.layout_panel.changed.connect(self._layouts_changed)
        self.base_panel = BasePanel(self, self._send)
        self.base_panel.before_save = self._ensure_layouts_saved
        self.events_panel = self._build_events()
        self.panels = {"themes": self.theme_panel, "zones": self.layout_panel,
                       "base": self.base_panel, "events": self.events_panel}
        for key, _ in self.TABS:
            self.panels[key].hide()
            stack_col.addWidget(self.panels[key])
        self.add(self.stack)
        self.body.addStretch(1)
        self.tabs.set("themes")
        self._select_panel("themes")
        self.ready = True

    # -- page life ----------------------------------------------------------------------

    def activated(self):
        if not self.ready:
            return
        self.reload()
        if self._current_panel is self.base_panel and self.base_panel.live.isChecked():
            self.base_panel._push_live()

    def deactivated(self):
        """Leaving the page: nothing may stay held on the hardware behind it."""
        if not self.ready:
            return
        self.base_panel.stop_live()
        self.layout_panel.stop_live()

    def show_tab(self, key: str):
        self.tabs.set(key)
        self._tab_changed(key)

    def _select_panel(self, key: str):
        current = self.panels[key]
        for panel in self.panels.values():
            if panel is not current:
                panel.hide()
        current.show()
        self._current_panel = current
        return current

    def _tab_changed(self, key: str):
        current = self._select_panel(key)
        # The preview deliberately survives the tab change. It stands in for the
        # default look, and alerts outrank it, so leaving it up while the alert
        # list is open shows exactly what the machine will do once saved --
        # whereas dropping it here made a half-finished default vanish from the
        # hardware just for looking at another tab.
        if current is not self.layout_panel:
            # Two priority-0 holds on the same surface would fight over it, and
            # which one won would look arbitrary. The zone tab keeps its preview
            # only while it is the tab you are looking at.
            self.layout_panel.stop_live()
        if current is self.base_panel and self.base_panel.live.isChecked():
            self.base_panel._push_live()

    def set_status(self, text: str):
        self.status.setText(text)

    # -- daemon -------------------------------------------------------------------------

    def _send(self, request: dict, quiet: bool = False) -> dict | None:
        try:
            return desktop.fx(request)
        except HubError as exc:
            if not quiet:
                self.set_status(f"errore: {exc}")
                self.ctx.toast(f"omen-fx: {exc}", "err")
            return None

    def reload(self) -> None:
        status = self._send({"cmd": "status"})
        st = (status or {}).get("status", {})
        if status:
            self.fast_write = bool(st.get("fast_write", True))
            # Match the daemon, so the preview blends the bar the same way.
            blend = st.get("bar_blend")
            if blend:
                E.surface.set_bar_blend(blend)
        reply = self._send({"cmd": "config", "action": "get"})
        if not reply:
            return
        self.theme_panel.load(reply)
        self.effects = reply.get("effects", {})
        self.triggers = reply.get("triggers", [])
        self.events_context.setText(f"Tema: {reply.get('active_theme') or 'aspetto personalizzato'}")
        self.layouts = reply.get("layouts", {}) or {}
        self.layout_panel.load(self.layouts)
        self.base_panel.layouts = self.layouts
        # Before any profile is loaded: an editor whose picker is still empty
        # cannot represent "this surface is on a layout", and committing that
        # empty state would overwrite the choice with a plain effect.
        self._publish_layouts()
        # The unexpanded profiles: the editor must see `layout = "name"` to
        # show which layout is chosen, not the zones it was expanded into.
        self.base_panel.load(reply.get("base_raw") or reply.get("base", {}))
        current = self.list.currentItem().text() if self.list.currentItem() else None
        # Clearing emits currentItemChanged(previous=old selection). Do not
        # commit the old editor into a freshly loaded/applied theme.
        self.list.blockSignals(True)
        self.list.clear()
        for name in sorted(self.effects):
            self.list.addItem(self._effect_item(name))
        self.list.blockSignals(False)
        selected = current if current in self.effects else next(iter(sorted(self.effects)), None)
        self._select_name(selected)
        if selected is None:
            self._select(None, None)
        self._dirty = False
        self.save_button.setText("Salva")
        warn = "" if st.get("available", True) else "driver non caricato: l'anteprima funziona, la barra no · "
        self.set_status(f"{warn}{len(self.effects)} effetti · {len(self.layouts)} layout · "
                        f"predefiniti in {reply.get('base_file')}")

    def _publish_layouts(self) -> None:
        """Offer the same layout names everywhere they can be chosen."""
        names = self.layout_panel.names()
        for editor in (self.editor, self.base_panel.editor):
            editor.set_layouts(names, self.layout_panel.layouts)

    def _layouts_changed(self) -> None:
        """A layout was edited: refresh every preview that may show it."""
        self.layouts = self.layout_panel.layouts
        self.base_panel.layouts = self.layouts
        self._publish_layouts()
        self.base_panel._preview()
        self._preview()

    def _ensure_layouts_saved(self) -> None:
        """Write pending layout edits before anything that refers to them.

        Saving an alert that names a layout the daemon has never seen would
        store a dangling reference, and the surface would simply stay dark.
        """
        if self.layout_panel.dirty:
            self.layout_panel.save()

    # -- themes -------------------------------------------------------------------------

    def _capture_theme(self):
        self._commit()
        self.base_panel._commit()
        return {"base": self.base_panel.profiles, "effects": self.effects,
                "layouts": self.layout_panel.layouts}

    def _prepare_theme(self):
        # Defaults may be live but not saved; switching replaces all editors.
        if not self.ctx.confirm("Applica tema",
                                "Sostituire aspetto predefinito, zone ed effetti attuali con questo "
                                "tema? Se vuoi tenere le modifiche correnti salvale prima come tema."):
            return False
        self.base_panel.live.setChecked(False)
        self.layout_panel.live.setChecked(False)
        self.base_panel.stop_live()
        self.layout_panel.stop_live()
        return True

    # -- events & effects ---------------------------------------------------------------

    def _build_events(self) -> QWidget:
        FW = self.FW
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        left = W.Card("Effetti del tema",
                      "Spunta gli effetti che questo tema può usare: togliere la spunta ne "
                      "conserva le impostazioni.")
        left.setFixedWidth(300)
        self.events_context = W.label("", "muted")
        left.add(self.events_context)
        self.list = QListWidget()
        self.list.setMinimumHeight(320)
        self.list.currentItemChanged.connect(self._select)
        self.list.itemChanged.connect(self._toggle_effect)
        left.add(self.list, 1)
        buttons = QHBoxLayout()
        for text, slot in (("Nuovo", self._add), ("Duplica", self._duplicate),
                           ("Elimina", self._remove)):
            b = QPushButton(text)
            if text == "Elimina":
                b.setObjectName("danger")
            b.clicked.connect(slot)
            buttons.addWidget(b)
        left.add(buttons)
        left.add(FW.note("«Salva» applica queste modifiche; per tenerle anche nella voce del "
                         "tema salva il tema nella scheda Temi."))
        row.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)

        right = W.Card()
        self.preview = FW.SurfacePreview()
        right.add(self.preview)
        self.event_source = W.label("", "muted", wrap=True)
        right.add(self.event_source)
        self.editor = FW.EffectEditor()
        self.editor.changed.connect(self._preview)
        self.editor.changed.connect(self._mark_dirty)
        right.add(self.editor, 1)
        self.play_button = QPushButton("▶  Prova effetto")
        self.play_button.setToolTip("Lo prova sulle superfici scelte, anche se è disattivato nel tema.")
        self.play_button.clicked.connect(self._play_on_bar)
        self.revert_button = QPushButton("Rileggi dal demone")
        self.revert_button.clicked.connect(self.reload)
        self.save_button = QPushButton("Salva", objectName="primary")
        self.save_button.clicked.connect(self._save)
        right.add(W.hrow(self.play_button, None, self.revert_button, self.save_button))
        row.addWidget(right, 1)
        return panel

    def _save(self) -> None:
        self._commit()
        self._ensure_layouts_saved()
        reply = self._send({"cmd": "config", "action": "save", "effects": self.effects})
        if reply:
            self._dirty = False
            self.save_button.setText("Salva")
            state = self._send({"cmd": "config", "action": "get"})
            if state:
                self.theme_panel.load(state)
            self.ctx.toast(f"{reply.get('saved')} effetti salvati in {reply.get('file')}", "ok")
            self.set_status("salvato")

    def _commit(self) -> None:
        item = self.list.currentItem()
        if item:
            self._store_editor(item)

    def _store_editor(self, item) -> None:
        # The list owns this switch; the editor may still carry its older value.
        spec = self.editor.spec()
        spec["enabled"] = item.checkState() == Qt.CheckState.Checked
        self.effects[item.text()] = spec

    def _effect_item(self, name):
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if self.effects[name].get("enabled", True)
                           else Qt.CheckState.Unchecked)
        item.setToolTip("Abilita questo effetto nel tema corrente")
        return item

    def _toggle_effect(self, item) -> None:
        if self._loading_effect or item.text() not in self.effects:
            return
        self.effects[item.text()]["enabled"] = item.checkState() == Qt.CheckState.Checked
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        if self._loading_effect or self._dirty:
            return
        self._dirty = True
        self.save_button.setText("Salva  •")
        self.set_status("modifiche non salvate: gli effetti usano il valore salvato finché non premi «Salva»")

    def _select(self, current, previous) -> None:
        if previous and previous.text() in self.effects:
            self._store_editor(previous)
        self.editor.setEnabled(current is not None)
        self.play_button.setEnabled(current is not None)
        if current:
            self._loading_effect = True
            self.editor.load(self.effects.get(current.text(), dict(self.FW.DEFAULTS)))
            self._loading_effect = False
            events = sorted({r.get("event", "") for r in self.triggers
                             if r.get("effect") == current.text()})
            self.event_source.setText("Attivato da: " + ", ".join(events) if events else
                                      "Effetto manuale · provalo qui o lancialo da riga di comando "
                                      "(omen-fx play). Gli eventi automatici si associano in config.toml.")
        else:
            self.event_source.setText("Aggiungi un effetto per cominciare.")
            self.preview.play(None)

    def _select_name(self, name: str | None) -> None:
        if not name:
            return
        for i in range(self.list.count()):
            if self.list.item(i).text() == name:
                self.list.setCurrentRow(i)
                return

    def _ask_name(self, title, text="") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Nome:", text=text)
        name = (name or "").strip()
        return name if ok and name else None

    def _add(self) -> None:
        name = self._ask_name("Nuovo effetto")
        if not name:
            return
        if name in self.effects:
            self.ctx.toast(f"Esiste già un effetto «{name}»", "warn")
            self._select_name(name)
            return
        self.effects[name] = dict(self.FW.DEFAULTS)
        self.list.addItem(self._effect_item(name))
        self.list.sortItems()
        self._select_name(name)
        self._mark_dirty()

    def _duplicate(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        name = self._ask_name("Duplica effetto", f"{item.text()}-2")
        if not name:
            return
        if name in self.effects:
            self.ctx.toast(f"Esiste già un effetto «{name}»", "warn")
            return
        self._commit()
        self.effects[name] = dict(self.effects[item.text()])
        self.list.addItem(self._effect_item(name))
        self.list.sortItems()
        self._select_name(name)
        self._mark_dirty()

    def _remove(self) -> None:
        item = self.list.currentItem()
        if not item or not self.ctx.confirm("Elimina effetto", f"Eliminare «{item.text()}»?"):
            return
        self.effects.pop(item.text(), None)
        self.list.takeItem(self.list.row(item))
        self._mark_dirty()

    def _effect(self):
        try:
            return self.FW.build_effect(self.editor.spec(), self.layouts)
        except ValueError as exc:
            self.set_status(str(exc))
            return None

    def _preview(self) -> None:
        effect = self._effect()
        self.editor.describe(effect, self.fast_write)
        self.preview.play(effect)

    def _play_on_bar(self) -> None:
        self._commit()
        item = self.list.currentItem()
        if not item:
            return
        reply = self._send({"cmd": "play", "spec": self.editor.spec(),
                            "key": f"gui-{item.text()}", "priority": 95})
        if reply:
            self.set_status(f"in riproduzione: {item.text()}")


# -- themes ---------------------------------------------------------------------------------

class ThemePanel(QWidget):
    """A library of complete looks, with one explicit apply action."""
    applied = pyqtSignal()

    def __init__(self, page, send, capture, prepare_apply):
        super().__init__()
        self.page = page
        self._send, self._capture, self._prepare = send, capture, prepare_apply
        self.themes = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        card = W.Card("I tuoi temi di luce",
                      "Un tema tiene insieme l'aspetto predefinito, le sue zone e gli effetti "
                      "abilitati. Scegline uno per vederlo in anteprima; «Applica» cambia "
                      "tastiera e barra.")
        self.active = W.Pill()
        card.header.addWidget(self.active, 0, Qt.AlignmentFlag.AlignTop)
        content = QHBoxLayout()
        content.setSpacing(16)
        self.list = QListWidget()
        self.list.setFixedWidth(260)
        self.list.setMinimumHeight(260)
        self.list.currentTextChanged.connect(self._select)
        content.addWidget(self.list)
        right = QVBoxLayout()
        self.preview = page.FW.SurfacePreview(height=240)
        right.addWidget(self.preview)
        self.details = W.label("", "muted", wrap=True)
        right.addWidget(self.details)
        right.addStretch()
        content.addLayout(right, 1)
        card.add(content, 1)
        save = QPushButton("Salva le modifiche come tema…")
        save.clicked.connect(self._save)
        self.remove = QPushButton("Elimina…", objectName="danger")
        self.remove.clicked.connect(self._delete)
        self.apply = QPushButton("Applica tema", objectName="primary")
        self.apply.clicked.connect(self._apply)
        card.add(W.hrow(save, self.remove, None, self.apply))
        self.note = W.label("Modifica Predefinito, Zone ed Eventi, poi salvali qui come tema "
                            "con un nome.", "faint", wrap=True)
        card.add(self.note)
        root.addWidget(card)
        root.addStretch(1)

    def load(self, reply):
        self.themes = reply.get("themes", {})
        name = reply.get("active_theme")
        if name:
            self.active.set("ok", f"● attivo: {name}" + (" · modificato" if reply.get("theme_modified") else ""))
        else:
            self.active.set("off", "○ aspetto personalizzato")
        selected = self.list.currentItem()
        keep = selected.text() if selected else (name or "")
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems(sorted(self.themes))
        self.list.blockSignals(False)
        if keep in self.themes:
            self.list.setCurrentRow(sorted(self.themes).index(keep))
        elif self.list.count():
            self.list.setCurrentRow(0)
        self._select(self.list.currentItem().text() if self.list.currentItem() else "")

    def _select(self, name):
        theme = self.themes.get(name)
        self.apply.setEnabled(theme is not None)
        self.remove.setEnabled(theme is not None)
        if not theme:
            self.preview.stop()
            self.details.setText("Nessun tema salvato: la luce attuale resta com'è.")
            return
        try:
            self.preview.play(E.themes.ThemePreview(theme))
        except (ValueError, TypeError, KeyError):
            self.preview.stop()
        effects = theme.get("effects", {})
        enabled = sum(spec.get("enabled", True) is not False for spec in effects.values())
        self.details.setText(f"{len(theme.get('layouts', {}))} layout a zone · "
                             f"{enabled}/{len(effects)} effetti abilitati\n"
                             "Contiene i profili predefiniti di tastiera e barra; interruttori, "
                             "tempi e priorità degli effetti sono salvati col tema.")

    def _save(self):
        name, ok = QInputDialog.getText(self, "Salva tema", "Nome (lettere, numeri, - o _):")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.themes and not self.page.ctx.confirm(
                "Sostituisci tema", f"Sostituire il tema salvato «{name}»?"):
            return
        reply = self._send({"cmd": "config", "action": "save_theme", "name": name,
                            "theme": deepcopy(self._capture())})
        if reply:
            state = self._send({"cmd": "config", "action": "get"})
            if state:
                self.load(state)
                if name in self.themes:
                    self.list.setCurrentRow(sorted(self.themes).index(name))
            self.page.ctx.toast(f"Tema «{name}» salvato. Applicalo quando vuoi che diventi il predefinito.", "ok")

    def _apply(self):
        item = self.list.currentItem()
        if item is None or not self._prepare():
            return
        reply = self._send({"cmd": "config", "action": "apply_theme", "name": item.text()})
        if reply:
            self.applied.emit()
            self.page.ctx.toast(f"Tema «{item.text()}» applicato: profili, zone ed effetti cambiati insieme", "ok")

    def _delete(self):
        item = self.list.currentItem()
        if item is None:
            return
        name = item.text()
        if not self.page.ctx.confirm("Elimina tema", f"Eliminare «{name}» dalla libreria? "
                                     "La luce attiva resta com'è."):
            return
        if self._send({"cmd": "config", "action": "delete_theme", "name": name}):
            state = self._send({"cmd": "config", "action": "get"})
            if state:
                self.load(state)


# -- zones ----------------------------------------------------------------------------------

class LayoutPanel(QWidget):
    """Layouts, their zones, and the effect inside each zone.

    A layout is an ordered stack of zones, so the list *is* the priority: zone 1
    is the background and everything below it paints on top. That is why the
    list is reorderable and numbered rather than carrying a priority field.
    """

    changed = pyqtSignal()
    LIVE_KEY = "gui-layout-live"

    def __init__(self, page, send, editor_factory):
        super().__init__()
        self.page = page
        FW = page.FW
        self.lay = E.lay
        self._send = send
        self._loading = False
        self.layouts: dict = {}
        self._name: str | None = None
        # Whether there are edits the daemon has not been told about yet.
        self.dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        top = W.Card("Layout a zone",
                     "Disegna sulla griglia le zone di tastiera e barra; ognuna ha il suo effetto.")
        self.picker = QComboBox()
        self.picker.setMinimumWidth(240)
        self.picker.currentIndexChanged.connect(self._pick)
        bar = [W.label("Layout"), self.picker]
        for text, slot in (("Nuovo", self._new), ("Duplica", self._duplicate),
                           ("Rinomina", self._rename), ("Elimina", self._delete)):
            b = QPushButton(text)
            if text == "Elimina":
                b.setObjectName("danger")
            b.clicked.connect(slot)
            bar.append(b)
        top.add(W.hrow(*bar, None))
        self.grid = FW.ZoneGrid()
        self.grid.changed.connect(self._grid_changed)
        top.add(self.grid)
        self.live = FW.Switch("Mostra dal vivo sull'hardware",
                              "Tiene tastiera e barra sul layout che stai disegnando, finché "
                              "resti su questa scheda.")
        self.live.toggled.connect(self._live_toggled)
        self.save_button = QPushButton("Salva i layout", objectName="primary")
        self.save_button.clicked.connect(self.save)
        top.add(W.hrow(self.live, None, self.save_button))
        root.addWidget(top)

        split = QHBoxLayout()
        split.setSpacing(16)
        left = W.Card("Zone", "La prima sta sotto, le altre dipingono sopra.")
        left.setFixedWidth(340)
        self.zones = QListWidget()
        self.zones.setMinimumHeight(220)
        self.zones.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.zones.currentRowChanged.connect(self._select_zone)
        left.add(self.zones, 1)
        buttons = []
        for text, tip, slot in (("+", "aggiungi zona", self._add_zone),
                                ("−", "togli zona", self._remove_zone),
                                ("▲", "sposta sotto (prima)", lambda: self._move(-1)),
                                ("▼", "sposta sopra (dopo)", lambda: self._move(1))):
            b = QPushButton(text)
            b.setFixedWidth(38)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            buttons.append(b)
        self.rename_zone = QPushButton("Rinomina")
        self.rename_zone.clicked.connect(self._rename_zone)
        left.add(W.hrow(*buttons, None, self.rename_zone, spacing=6))
        left.add(FW.note("Una zona può prendere solo tasti, solo segmenti della barra o "
                         "entrambi: quello che dipingi è quello che illumina."))
        self.note = FW.note()
        left.add(self.note)
        split.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)

        right = W.Card("Effetto della zona")
        self.transparent = FW.Switch(
            "Mostra lo strato sotto quando questo effetto si attenua o si spegne",
            wrap=True, tip="Tiene visibile la zona inferiore tra un lampeggio e l'altro e sotto le scie. "
            "Disattivato: colore di spento opaco.")
        self.transparent.toggled.connect(self._transparency_changed)
        right.add(self.transparent)
        self.editor = editor_factory()
        self.editor.changed.connect(self._editor_changed)
        right.add(self.editor, 1)
        split.addWidget(right, 1)
        root.addLayout(split)

        # Animates the grid with the composed output and feeds the live preview.
        self._clock = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(200)
        self._push_timer.timeout.connect(self._push_live)

    # -- data -------------------------------------------------------------

    @property
    def zone_list(self) -> list:
        if self._name is None:
            return []
        return self.layouts.get(self._name, {}).get("zones", [])

    @property
    def zone(self) -> dict | None:
        row = self.zones.currentRow()
        zones = self.zone_list
        return zones[row] if 0 <= row < len(zones) else None

    def load(self, layouts: dict) -> None:
        # "bar_source" is retired -- the bar is driven by the zones' own bar
        # cells now. Dropped on the way in so files written before the change
        # stop carrying it forward.
        self.layouts = {
            k: {**{kk: vv for kk, vv in v.items() if kk != "bar_source"},
                "zones": [dict(z) for z in v.get("zones", [])]}
            for k, v in (layouts or {}).items()}
        self.dirty = False
        self.save_button.setText("Salva i layout")
        self._refresh_picker(self._name)

    def names(self) -> list[str]:
        return sorted(self.layouts)

    def _refresh_picker(self, keep: str | None) -> None:
        self._loading = True
        self.picker.clear()
        for name in self.names():
            self.picker.addItem(name)
        if keep and keep in self.layouts:
            self.picker.setCurrentText(keep)
        self._loading = False
        self._pick()

    def _pick(self, *_args) -> None:
        if self._loading:
            return
        self._name = self.picker.currentText() or None
        self._refresh_zones(0)

    def _ask(self, title, text=""):
        name, ok = QInputDialog.getText(self, title, "Nome:", text=text)
        name = (name or "").strip()
        return name if ok and name else None

    def _new(self) -> None:
        name = self._ask("Nuovo layout")
        if not name:
            return
        if name in self.layouts:
            self.page.ctx.toast(f"«{name}» esiste già", "warn")
            return
        lay = self.lay
        self.layouts[name] = {
            "zones": [{"name": "sfondo", "kind": "solid", "color": "202060",
                       "mask": lay.dump_mask({(r, c) for r in range(lay.GRID_ROWS)
                                              for c in range(lay.GRID_COLS)}),
                       "bar": lay.dump_bar({0, 1, 2, 3})}],
        }
        self._refresh_picker(name)
        self._touch()

    def _duplicate(self) -> None:
        if self._name is None:
            return
        name = self._ask("Duplica layout", f"{self._name}-2")
        if not name or name in self.layouts:
            return
        source = self.layouts[self._name]
        self.layouts[name] = {**source, "zones": [dict(z) for z in source.get("zones", [])]}
        self._refresh_picker(name)
        self._touch()

    def _rename(self) -> None:
        if self._name is None:
            return
        name = self._ask("Rinomina layout", self._name)
        if not name or name == self._name or name in self.layouts:
            return
        self.layouts[name] = self.layouts.pop(self._name)
        self._refresh_picker(name)
        self._touch()

    def _delete(self) -> None:
        if self._name is None:
            return
        if not self.page.ctx.confirm("Elimina layout", f"Eliminare il layout «{self._name}»?"):
            return
        self.layouts.pop(self._name, None)
        self._refresh_picker(None)
        self._touch()

    # -- zones ------------------------------------------------------------

    def _zone_text(self, i, zone):
        label = zone.get("name") or f"zona {i + 1}"
        return f"{i + 1}.  {label}   ({zone.get('kind', 'solid')})"

    def _refresh_zones(self, row: int) -> None:
        self._loading = True
        self.zones.clear()
        for i, zone in enumerate(self.zone_list):
            item = QListWidgetItem(self._zone_text(i, zone))
            swatch = self.page.FW.zone_color(zone, i)
            # Keep it legible on the list background whatever the zone is.
            if W.T.dark:
                swatch = swatch if swatch.lightness() > 90 else swatch.lighter(220)
            else:
                swatch = swatch if swatch.lightness() < 150 else swatch.darker(180)
            item.setForeground(swatch)
            self.zones.addItem(item)
        self._loading = False
        if self.zones.count():
            self.zones.setCurrentRow(min(max(0, row), self.zones.count() - 1))
        else:
            self._select_zone(-1)

    def _select_zone(self, row: int) -> None:
        zone = self.zone
        self.grid.set_zones(self.zone_list, row)
        self.editor.setEnabled(zone is not None)
        self.transparent.setEnabled(zone is not None)
        if zone is None:
            self._refresh_preview()
            return
        self._loading = True
        self.transparent.setChecked(zone.get("off_transparent", "off_color" not in zone))
        self.editor.load({k: v for k, v in zone.items() if k not in self.lay.ZONE_KEYS})
        self._loading = False
        self._refresh_preview()

    def _add_zone(self) -> None:
        if self._name is None:
            self.page.ctx.toast("Crea prima un layout, con «Nuovo»", "warn")
            return
        zones = self.zone_list
        zones.append({"name": f"zona {len(zones) + 1}", "kind": "solid",
                      "color": "FF7A00", "mask": self.lay.dump_mask(set())})
        self._refresh_zones(len(zones) - 1)
        self._touch()
        self.note.setText("Zona aggiunta: disegnala sulla griglia qui sopra. Essendo "
                          "l'ultima, dipinge sopra tutte le altre.")

    def _remove_zone(self) -> None:
        row = self.zones.currentRow()
        zones = self.zone_list
        if not (0 <= row < len(zones)):
            return
        zones.pop(row)
        self._refresh_zones(row - 1)
        self._touch()

    def _move(self, delta: int) -> None:
        row = self.zones.currentRow()
        zones = self.zone_list
        target = row + delta
        if not (0 <= row < len(zones)) or not (0 <= target < len(zones)):
            return
        zones[row], zones[target] = zones[target], zones[row]
        self._refresh_zones(target)
        self._touch()

    def _rename_zone(self) -> None:
        zone = self.zone
        if zone is None:
            return
        name, ok = QInputDialog.getText(self, "Rinomina zona", "Nome:",
                                        text=str(zone.get("name", "")))
        if not ok:
            return
        zone["name"] = (name or "").strip()
        self._refresh_zones(self.zones.currentRow())
        self._touch()

    # -- edits ------------------------------------------------------------

    def _transparency_changed(self, checked):
        if self._loading or self.zone is None:
            return
        self.zone["off_transparent"] = checked
        self._touch()

    def _editor_changed(self) -> None:
        zone = self.zone
        if zone is None or self._loading:
            return
        keep = {k: zone[k] for k in self.lay.ZONE_KEYS if k in zone}
        zone.clear()
        zone.update(self.editor.spec())
        zone.update(keep)
        row = self.zones.currentRow()
        item = self.zones.item(row)
        if item is not None:
            item.setText(self._zone_text(row, zone))
        self._touch()

    def _grid_changed(self) -> None:
        self._touch()

    def _touch(self) -> None:
        self.dirty = True
        self.save_button.setText("Salva i layout  •")
        self.grid.set_zones(self.zone_list, self.zones.currentRow())
        self._refresh_preview()
        self.changed.emit()
        if self.live.isChecked():
            self._push_timer.start()

    # -- preview ----------------------------------------------------------

    def spec(self) -> dict | None:
        """The layout being edited, as a playable effect spec."""
        if self._name is None:
            return None
        spec = dict(self.layouts[self._name])
        spec["kind"] = "layout"
        return spec

    def _refresh_preview(self) -> None:
        spec = self.spec()
        if not spec or not spec.get("zones"):
            self._timer.stop()
            self.grid.set_frame([], [])
            return
        if not self._timer.isActive() and self.isVisible():
            self._timer.start(50)
        self._advance(step=False)

    def _advance(self, step: bool = True) -> None:
        spec = self.spec()
        if not spec:
            return
        if step:
            self._clock += 0.05
        try:
            effect = E.fx.build(spec)
        except ValueError:
            return
        self.grid.set_frame(effect.sample(E.surface.preview_key_points(), self._clock),
                            E.surface.bar_frame(effect, self._clock))

    # The animation only needs to run while the tab is on screen; stopping it
    # otherwise keeps an idle hub from sampling effects twenty times a second.
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_preview()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    # -- hardware ---------------------------------------------------------

    def _live_toggled(self, on: bool) -> None:
        if on:
            self._push_live()
        else:
            self.stop_live()

    def stop_live(self) -> None:
        self._push_timer.stop()
        self._send({"cmd": "release", "key": self.LIVE_KEY}, quiet=True)

    def _push_live(self) -> None:
        if not self.live.isChecked():
            return
        spec = self.spec()
        if not spec or not spec.get("zones"):
            return
        # A stand-in for the default look, not an alert: it must not wake a
        # surface the idle dim has put down.
        self._send({"cmd": "play", "spec": spec, "key": self.LIVE_KEY,
                    "target": "both", "priority": 0, "hold": True, "timeout": 0,
                    "stand_in": True})

    def save(self) -> None:
        reply = self._send({"cmd": "config", "action": "save_layouts", "layouts": self.layouts})
        if reply:
            self.dirty = False
            self.save_button.setText("Salva i layout")
            self.page.ctx.toast(f"{reply.get('saved')} layout salvati in {reply.get('file')}", "ok")
            self.changed.emit()


# -- default look ---------------------------------------------------------------------------

class BasePanel(QWidget):
    """The default look of each surface -- what shows when no alert is running.

    Kept apart from the alert list on purpose: these are not queued, have no
    priority and never end, so they are a different kind of thing. They live in
    their own file (`base.toml`), the same arrangement as the alerts, so the
    hand-written config.toml is never rewritten by a program.

    The mirror switch belongs to the *keyboard* section even though what it
    stores is the bar's profile, because that is where the decision is made:
    "the bar copies the keyboard". While it is on, the bar has no look of its
    own, so its section is locked rather than silently ignored.
    """

    # One held job per surface: the tab is about the whole machine, so what it
    # shows must not depend on which section happens to be selected. Previewing
    # only the selected surface made the other one snap back to its stored
    # profile the moment you switched -- which read as the lighting resetting.
    LIVE_KEYS = {"keys": "gui-base-live-keys", "bar": "gui-base-live-bar"}

    def __init__(self, page, send):
        super().__init__()
        self.page = page
        FW = page.FW
        self.lay = E.lay
        self._send = send
        self._loading = False
        self.profiles: dict = {"keys": {}, "bar": {}}
        # Filled in by reload: needed to resolve a profile that names a layout
        # instead of describing an effect.
        self.layouts: dict = {}
        # Hook for saving pending layout edits first; see _ensure_layouts_saved.
        self.before_save = None
        # What to put back on the bar when the mirror is switched off again,
        # so toggling it does not throw the bar's colours away.
        self._bar_backup: dict = {}

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)

        left = W.Card("Aspetto predefinito",
                      "Quello che mostrano tastiera e barra quando nessun avviso è in corso.")
        left.setFixedWidth(460)
        self.surface = W.Segmented([("keys", "Tastiera · 120 tasti"), ("bar", "Barra · 4 zone")])
        self.surface.set("keys")
        self.surface.changed.connect(lambda _k: self._switch())
        left.add(self.surface)
        self.preview = FW.SurfacePreview(height=220)
        left.add(self.preview)
        self.managed = FW.Switch(
            "omen-fx gestisce questa superficie",
            "Spento, omen-fx non tocca affatto questa superficie.\n"
            "Per la tastiera vuol dire non prendere mai la LampArray: il\n"
            "firmware tiene la sua illuminazione e la barra non viene copiata.")
        self.managed.toggled.connect(self._toggle)
        left.add(self.managed)
        self.mirror = FW.Switch(
            "La barra copia la tastiera",
            "Mentre omen-fx guida la tastiera il firmware la copia già sulla\n"
            "barra. Così non costa nessuna scrittura WMI e la tastiera resta a\n"
            "30 fps pieni invece di ~24. Finché è attivo la barra non ha un aspetto suo.")
        self.mirror.toggled.connect(self._toggle_mirror)
        left.add(self.mirror)
        self.note = FW.note()
        left.add(self.note)
        left.lay.addStretch(1)
        self.live = FW.Switch(
            "Mostra dal vivo sull'hardware",
            "Tiene entrambe le superfici su quello che stai preparando finché resti\n"
            "in questa pagina. Gli avvisi ci suonano sopra, come faranno davvero.\n"
            "È solo un'anteprima: «Salva come predefinito» lo rende stabile.")
        self.live.setChecked(True)
        self.live.toggled.connect(self._live_toggled)
        self.save_button = QPushButton("Salva come predefinito", objectName="primary")
        self.save_button.clicked.connect(self.save)
        left.add(W.hrow(self.live, None, self.save_button))
        row.addWidget(left)

        right = W.Card("Effetto")
        self.editor = FW.EffectEditor(base_mode=True)
        self.editor.changed.connect(self._preview)
        right.add(self.editor, 1)
        row.addWidget(right, 1)

        # Sliders fire continuously; without this the socket would get a
        # request per pixel of drag.
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(150)
        self._push_timer.timeout.connect(self._push_live)

    # -- state ------------------------------------------------------------

    @property
    def current(self) -> str:
        return self.surface.value() or "keys"

    @property
    def mirrored(self) -> bool:
        return str(self.profiles.get("bar", {}).get("kind", "")).lower() == "mirror"

    @property
    def keys_layout(self) -> str | None:
        """The layout the keyboard is on, if it is on one."""
        spec = self.profiles.get("keys") or {}
        if str(spec.get("kind", "")).lower() != self.lay.LAYOUT_KIND:
            return None
        return str(spec.get("layout") or "") or None

    def _enforce_layout_bar(self) -> None:
        """Keep the bar on the same layout as the keyboard.

        A layout covers both surfaces -- its four bar cells are painted in the
        Zone tab beside the keys -- so the mirror has no job left, and the bar
        goes on the layout instead of copying the keyboard.

        It must *follow* later changes too, not just the first one. Firing only
        while the bar was still mirrored left it pointing at whichever layout
        it was put on first, so switching the keyboard to another one left the
        bar playing the old layout's effect.

        A bar given an effect of its own is not touched: only a mirrored bar,
        or one already on a layout, follows.
        """
        name = self.keys_layout
        if name is None:
            return
        bar = self.profiles.get("bar") or {}
        kind = str(bar.get("kind", "")).lower()
        if kind == self.lay.LAYOUT_KIND and bar.get("layout") == name:
            return                          # already in step
        if kind not in ("mirror", self.lay.LAYOUT_KIND):
            return                          # the bar has a look of its own
        self.profiles["bar"] = {"kind": self.lay.LAYOUT_KIND, "layout": name}
        self._bar_backup = dict(self.profiles["bar"])
        was_loading = self._loading
        self._loading = True
        self.mirror.setChecked(False)
        self._loading = was_loading

    def load(self, profiles: dict) -> None:
        self.profiles = {"keys": dict(profiles.get("keys") or {}),
                         "bar": dict(profiles.get("bar") or {})}
        if not self.mirrored and self.profiles["bar"]:
            self._bar_backup = dict(self.profiles["bar"])
        # A hand-edited config may still pair a keyboard layout with a mirrored
        # bar; correct it on the way in rather than showing an impossible state.
        self._enforce_layout_bar()
        self._show(self.current)

    def _fallback(self, name: str) -> dict:
        return {"kind": "gradient", "colors": ["001A40", "2A0A60"],
                # 100 = "as bright as the desktop's keyboard-backlight slider
                # says"; that slider is the master, so a default profile has no
                # reason to hold itself back.
                "cycles": 0, "brightness": 100 if name == "keys" else -1}

    def _show(self, name: str) -> None:
        self._loading = True
        spec = self.profiles.get(name) or {}
        is_mirror = str(spec.get("kind", "")).lower() == "mirror"

        # The switch lives with the keyboard; the bar only suffers it.
        self.mirror.setVisible(name == "keys")
        self.mirror.setChecked(self.mirrored)
        self.mirror.setEnabled(self.keys_layout is None)
        self.managed.setVisible(not (name == "bar" and is_mirror))
        self.managed.setChecked(bool(spec) and not is_mirror)
        self.editor.load(spec if spec and not is_mirror else
                         (self._bar_backup if name == "bar" and self._bar_backup
                          else self._fallback(name)))
        self._loading = False
        self._sync()
        self._preview()

    def _sync(self) -> None:
        """Enable what can be edited, and say why when something cannot."""
        name = self.current
        mirrored = self.mirrored
        # Make the blocked bar section visible as blocked rather than hiding it.
        bar_btn = self.surface.buttons["bar"]
        bar_btn.setEnabled(not mirrored)
        bar_btn.setText("Barra · copia la tastiera" if mirrored else "Barra · 4 zone")

        if name == "bar" and mirrored:
            self.editor.setEnabled(False)
            self.live.setEnabled(False)
            self.note.setText(
                "La barra sta copiando la tastiera, quindi non ha un aspetto suo da "
                "impostare. Per darle dei colori torna su Tastiera e spegni «La barra "
                "copia la tastiera».")
            return

        self.mirror.setEnabled(self.keys_layout is None)
        managed = self.managed.isChecked()
        self.editor.setEnabled(managed)
        self.live.setEnabled(managed)
        if not managed:
            self.note.setText(
                "omen-fx non tocca questa superficie: la tiene il firmware."
                if name == "keys" else "omen-fx non tocca la barra luminosa.")
        elif self.keys_layout is not None:
            self.note.setText(
                f"La tastiera è sul layout «{self.keys_layout}», che copre anche la "
                "barra: dipingi i suoi 4 segmenti nella scheda Zone, accanto ai tasti. "
                "Per questo la copia non serve ed è spenta.")
        elif name == "keys" and mirrored:
            self.note.setText("La barra copia questo aspetto, senza scritture WMI.")
        else:
            self.note.setText("")

    def _commit(self) -> None:
        if self._loading:
            return
        name = self.current
        if name == "bar" and self.mirrored:
            return                      # nothing of the bar's own to store
        if not self.managed.isChecked():
            self.profiles[name] = {}
        else:
            self.profiles[name] = self.editor.spec()
        if name == "bar":
            self._bar_backup = dict(self.profiles["bar"])
        self._enforce_layout_bar()

    def _switch(self) -> None:
        # Both surfaces stay previewed; only the editor moves.
        self._show(self.current)

    def _toggle(self, *_a) -> None:
        if self._loading:
            return
        self._commit()
        self._sync()
        self._preview()

    def _toggle_mirror(self, checked: bool) -> None:
        if self._loading:
            return
        self._commit()          # save whatever the keyboard section holds
        if checked:
            if not self.mirrored and self.profiles.get("bar"):
                self._bar_backup = dict(self.profiles["bar"])
            self.profiles["bar"] = {"kind": "mirror"}
        else:
            # Hand the bar back with something to show, so the section is
            # immediately usable instead of arriving switched off.
            self.profiles["bar"] = (dict(self._bar_backup) if self._bar_backup
                                    else self._fallback("bar"))
        self._sync()
        self._preview()

    def _live_toggled(self, on: bool) -> None:
        if on:
            self._push_live()
        else:
            self.stop_live()

    def stop_live(self) -> None:
        """Give both surfaces back to whatever is actually configured."""
        self._push_timer.stop()
        for key in self.LIVE_KEYS.values():
            self._send({"cmd": "release", "key": key}, quiet=True)

    def _push_live(self) -> None:
        """Put both surfaces on what the tab currently describes."""
        if not self.live.isChecked() or not self.page.isVisible():
            return
        for name, key in self.LIVE_KEYS.items():
            spec = dict(self.profiles.get(name) or {})
            # An unmanaged surface, or a bar set to mirror, is one we must not
            # paint: release so the firmware (or the saved profile) has it.
            if not spec or str(spec.get("kind", "")).lower() == "mirror":
                self._send({"cmd": "release", "key": key}, quiet=True)
                continue
            spec["cycles"] = 0          # a preview must not end on its own
            # timeout 0: held until we release it, so it cannot expire behind
            # the user's back and look like the lighting resetting itself.
            # Priority 0: the preview stands in for the *default* look, so every
            # alert still interrupts it, and being a hold it comes back after.
            # stand_in says the same to the idle dim: this is not an alert, so
            # it must not wake a dimmed surface.
            self._send({"cmd": "play", "spec": spec, "key": key,
                        "target": name, "priority": 0,
                        "hold": True, "timeout": 0, "stand_in": True})

    def _preview(self) -> None:
        self._commit()
        self._sync()
        if self.live.isChecked():
            self._push_timer.start()
        name = self.current
        spec = self.profiles.get(name) or {}
        if name == "bar" and self.mirrored:
            # Show what the bar will actually display: the keyboard's look.
            spec = self.profiles.get("keys") or {}
        if not spec or str(spec.get("kind", "")).lower() == "mirror":
            self.preview.stop()
            return
        try:
            self.preview.play(self.page.FW.build_effect(spec, self.layouts))
        except ValueError:
            self.preview.stop()

    def save(self) -> None:
        self._commit()
        if self.before_save is not None:
            self.before_save()
        reply = self._send({"cmd": "config", "action": "save_base", "base": self.profiles})
        if reply:
            # Drop the preview so what stays on screen is the saved profile
            # itself, not an overlay that happens to look like it.
            self.stop_live()
            self.page.ctx.toast(f"Salvato in {reply.get('file')}: ora è il predefinito", "ok")
            self.note.setText(f"salvato in {reply.get('file')}")
