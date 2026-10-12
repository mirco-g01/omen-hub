"""Light studio building blocks: colour list, gradient handles, the animated
preview of both surfaces, the effect form and the paintable zone grid.

Ported from omen-fx-gui (omen_fx/gui.py, zonegui.py) to the hub's look. The
behaviour is kept line for line -- every comment explaining *why* a knob is
hidden or a key dropped came from a bug found there -- only the widgets and the
words changed. Effects are built by omen-fx's own engine (see fxengine), so the
preview cannot disagree with the daemon.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import (QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout,
                             QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
                             QSpinBox, QVBoxLayout, QWidget)

from .. import fxengine as E
from . import widgets as W

if not E.load():                       # the studio page checks first; this is a guard
    raise ImportError(E.ERROR)
fx, lay, led, surface = E.fx, E.lay, E.led, E.surface
ZONE_COUNT = led.ZONE_COUNT

# What a four-colour frame costs on the patched driver. Used only to warn when
# a requested duration is shorter than the hardware can actually deliver.
FRAME_FLOOR_MS = 30
FRAME_FLOOR_SLOW_MS = 860

# The lit surfaces are drawn on a dark "chassis" in both themes: LEDs read as
# light only over something dark.
CHASSIS = QColor(18, 18, 20)
CHASSIS_LINE = QColor(44, 44, 50)
CHASSIS_TEXT = QColor(120, 120, 128)

TARGETS = (("both", "Entrambe"), ("bar", "Barra"), ("keys", "Tastiera"))

DEFAULTS = {
    "kind": "center_out",
    "color": "00A8FF",
    "speed": 7,
    "cycles": 2,
    "smoothness": 0.45,
    "gamma": 2.2,
    "tail": 0.45,
    "priority": 50,
}


def build_effect(spec: dict, layouts: dict):
    """An effect from a spec that may name a layout instead of being one.

    Resolved through the same function the daemon uses, so the preview cannot
    disagree with the hardware about what a layout contains.
    """
    resolved = lay.expand(spec, layouts or {})
    if not resolved:
        return None
    return fx.build(resolved)


def _ink(color: str) -> str:
    """Readable text over a swatch: dark on light backgrounds, light on dark."""
    r, g, b = led.parse_color(color)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#FFFFFF"


# -- small pieces -------------------------------------------------------------------------

Switch = W.Switch


class Section(QWidget):
    """A titled block inside a card: caption over a two-column form."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 4, 0, 4)
        col.setSpacing(8)
        col.addWidget(W.label(title.upper(), "statLabel"))
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(16)
        self.form.setVerticalSpacing(10)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Only sliders, colour rows and the gradient stretch; spin boxes and
        # pickers keep their natural width instead of spanning the card.
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        col.addLayout(self.form)


def note(text: str = "") -> QLabel:
    return W.label(text, "faint", wrap=True)


# -- colours ------------------------------------------------------------------------------

class ColorList(QWidget):
    """One or more colours, in order.

    Effects read the list in two different ways -- ``gradient`` spreads it
    across the machine as stops, everything else cycles through it one colour
    per repetition -- so the editor keeps a single ordered list either way and
    lets the effect decide what it means.
    """

    changed = pyqtSignal()
    MAXIMUM = 8

    # How many colours each kind can actually use. Offering more would just be
    # a knob with no effect; offering fewer would hide a real feature.
    PER_KIND = {
        "solid": (1, 1), "static": (1, 1), "progress": (1, 1),
        "gradient": (2, 8), "alternate": (2, 2),
        "wave": (0, 0), "rainbow": (0, 0),
    }

    def __init__(self):
        super().__init__()
        self._limits = (1, self.MAXIMUM)
        self._colors: list[str] = ["00A8FF"]
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(6)
        self._buttons: list[QPushButton] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.add_button = QPushButton("+")
        self.add_button.setFixedWidth(34)
        self.add_button.setToolTip("aggiungi un colore")
        self.add_button.clicked.connect(self._add)
        self.del_button = QPushButton("−")
        self.del_button.setFixedWidth(34)
        self.del_button.setToolTip("togli l'ultimo colore")
        self.del_button.clicked.connect(self._remove)
        self._rebuild()

    def colors(self) -> list[str]:
        return list(self._colors)

    def set_kind(self, kind: str) -> None:
        """Clamp the palette to what this kind can use, silently."""
        low, high = self.PER_KIND.get(str(kind).lower(), (1, self.MAXIMUM))
        self._limits = (low, high)
        if high == 0:
            self._rebuild()
            return
        while len(self._colors) < low:
            self._colors.append(self._colors[-1] if self._colors else "00A8FF")
        del self._colors[high:]
        self._rebuild()

    def set_colors(self, colors) -> None:
        clean = []
        for value in (colors or ["00A8FF"]):
            try:
                clean.append(led.format_color(led.parse_color(value)))
            except (ValueError, TypeError):
                continue
        low, high = self._limits
        self._colors = clean[:max(1, high)] or ["00A8FF"]
        while len(self._colors) < low:
            self._colors.append(self._colors[-1])
        self._rebuild()

    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            w = item.widget()
            if w is not None and w not in (self.add_button, self.del_button):
                w.deleteLater()        # never setParent(None): it makes stray top-levels
        self._buttons = []
        for index, color in enumerate(self._colors):
            button = QPushButton(f"#{color}")
            button.setFixedSize(92, 30)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ background-color: #{color}; color: {_ink(color)};"
                f" border: 1px solid {W.T.border.name()}; border-radius: 8px;"
                f" font-family: monospace; padding: 0; }}"
                f"QPushButton:hover {{ border: 2px solid {W.T.accent.name()}; }}")
            button.setToolTip("clic per cambiare questo colore")
            button.clicked.connect(lambda _checked, i=index: self._pick(i))
            self._row.addWidget(button)
            self._buttons.append(button)
        self._row.addWidget(self.add_button)
        self._row.addWidget(self.del_button)
        self._row.addStretch()
        low, high = self._limits
        self.add_button.setVisible(high > low)
        self.del_button.setVisible(high > low)
        self.add_button.setEnabled(len(self._colors) < high)
        self.del_button.setEnabled(len(self._colors) > low)
        for button in self._buttons:
            button.setVisible(high > 0)

    def _pick(self, index: int) -> None:
        current = QColor(f"#{self._colors[index]}")
        chosen = QColorDialog.getColor(current, self, f"Colore {index + 1}")
        if not chosen.isValid():
            return
        self._colors[index] = f"{chosen.red():02X}{chosen.green():02X}{chosen.blue():02X}"
        self._rebuild()
        self.changed.emit()

    def _add(self) -> None:
        if len(self._colors) >= self._limits[1]:
            return
        self._colors.append(self._colors[-1])
        self._rebuild()
        self.changed.emit()

    def _remove(self) -> None:
        if len(self._colors) <= self._limits[0]:
            return
        self._colors.pop()
        self._rebuild()
        self.changed.emit()


class GradientBar(QWidget):
    """The gradient itself, with a draggable handle per colour stop.

    Dragging is the point: evenly spaced stops make every gradient look the
    same, and what usually reads well is uneven -- a long wash of one colour
    with a narrow band of another.
    """

    changed = pyqtSignal()
    MARGIN = 12
    BAR_H = 30
    GRIP = 7

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(58)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._colors: list[str] = ["001A40", "2A0A60"]
        self._stops: list[float] = [0.0, 1.0]
        self._drag: int | None = None
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("trascina le maniglie per spostare i punti del gradiente")

    def set_gradient(self, colors, stops=None) -> None:
        self._colors = list(colors) or ["000000", "FFFFFF"]
        count = len(self._colors)
        even = [i / (count - 1) for i in range(count)] if count > 1 else [0.0]
        if stops and len(stops) >= count:
            self._stops = sorted(max(0.0, min(1.0, float(v))) for v in stops[:count])
        else:
            self._stops = even
        self.update()

    def stops(self) -> list[float]:
        return [round(v, 4) for v in self._stops]

    def _x_for(self, stop: float) -> float:
        return self.MARGIN + stop * (self.width() - 2 * self.MARGIN)

    def _stop_for(self, x: float) -> float:
        usable = max(1, self.width() - 2 * self.MARGIN)
        return max(0.0, min(1.0, (x - self.MARGIN) / usable))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(W.T.surface2)
        painter.drawRoundedRect(QRectF(self.rect()), 10, 10)
        if len(self._colors) < 2:
            return
        gradient = QLinearGradient(self._x_for(0.0), 0, self._x_for(1.0), 0)
        for color, stop in zip(self._colors, self._stops):
            gradient.setColorAt(max(0.0, min(1.0, stop)), QColor(f"#{color}"))
        painter.setBrush(gradient)
        painter.setPen(W.T.border)
        painter.drawRoundedRect(QRectF(self.MARGIN, 6, self.width() - 2 * self.MARGIN, self.BAR_H), 6, 6)
        y = 6 + self.BAR_H + 4
        for index, (color, stop) in enumerate(zip(self._colors, self._stops)):
            x = int(self._x_for(stop))
            painter.setBrush(QColor(f"#{color}"))
            painter.setPen(W.T.accent if index == self._drag else W.T.muted)
            painter.drawEllipse(x - self.GRIP, y, self.GRIP * 2, self.GRIP * 2)

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        best, best_dx = None, 1e9
        for index, stop in enumerate(self._stops):
            dx = abs(self._x_for(stop) - x)
            if dx < best_dx:
                best, best_dx = index, dx
        if best is not None and best_dx <= 14:
            self._drag = best
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        # Keep the order: a handle may not pass its neighbours, or the ramp
        # would invert under the cursor.
        low = self._stops[self._drag - 1] + 0.005 if self._drag > 0 else 0.0
        high = (self._stops[self._drag + 1] - 0.005
                if self._drag < len(self._stops) - 1 else 1.0)
        self._stops[self._drag] = max(low, min(high, self._stop_for(event.position().x())))
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self.update()
            self.changed.emit()


# -- the preview --------------------------------------------------------------------------

class SurfacePreview(QWidget):
    """Both surfaces, animated exactly as the daemon would drive them.

    The effect is sampled here the same way the engine samples it -- one call
    per tick at the two surfaces' real coordinates -- so what you see is what
    the hardware will do, not a separate approximation of it.
    """

    TICK_MS = 33          # the keyboard's MinUpdateInterval
    OFF = (0, 0, 0)

    def __init__(self, height: int = 200):
        super().__init__()
        self.setMinimumHeight(height)
        self._effect = None
        self._t = 0.0
        self._keys = surface.preview_key_points()
        self._key_colors = [(0, 0, 0)] * len(self._keys)
        self._bar_colors = [(0, 0, 0)] * ZONE_COUNT
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def play(self, effect) -> None:
        if effect is None:
            self.stop()
            return
        self._effect = effect
        self._t = 0.0
        self._timer.stop()
        self._sample()
        if self.isVisible():
            self._timer.start(self.TICK_MS)

    def stop(self) -> None:
        self._timer.stop()
        self._effect = None
        self._key_colors = [(0, 0, 0)] * len(self._keys)
        self._bar_colors = [(0, 0, 0)] * ZONE_COUNT
        self.update()

    # Animate only while on screen: an idle hub must not sample effects 30
    # times a second behind another page.
    def showEvent(self, e):
        super().showEvent(e)
        if self._effect is not None and not self._timer.isActive():
            self._timer.start(self.TICK_MS)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def _advance(self) -> None:
        self._t += self.TICK_MS / 1000.0
        duration = getattr(self._effect, "duration", None)
        if duration is not None and self._t >= duration:
            self._t = 0.0     # loop the preview so it stays watchable
        self._sample()

    def _sample(self) -> None:
        try:
            keys = self._effect.sample(self._keys, self._t)
            bar = surface.bar_frame(self._effect, self._t)
        except Exception:
            self._timer.stop()
            return
        # A layout leaves unclaimed cells transparent. In a default profile
        # nothing is underneath, which is exactly how the hardware will show
        # it, so they are drawn off rather than skipped.
        self._key_colors = [c if c is not None else self.OFF for c in keys]
        self._bar_colors = [c if c is not None else self.OFF for c in bar]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(CHASSIS)
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)

        margin = 14
        label_h = 14
        usable_h = self.height() - 2 * margin - label_h
        keys_h = usable_h * 0.62
        bar_h = usable_h - keys_h - 10
        width = self.width() - 2 * margin

        # -- keyboard: 20 columns x 6 rows, laid out on the real coordinates
        cols, rows = 20, 6
        cell_w = width / cols
        cell_h = keys_h / rows
        for i, (r, g, b) in enumerate(self._key_colors):
            col, row = i % cols, i // cols
            x = margin + col * cell_w
            y = margin + row * cell_h
            painter.setBrush(QColor(r, g, b))
            painter.drawRoundedRect(QRectF(x + 1.5, y + 1.5, cell_w - 3, cell_h - 3), 3, 3)

        # -- light bar: four wide segments under it
        top = margin + keys_h + 10
        gap = 6
        seg_w = (width - gap * (ZONE_COUNT - 1)) / ZONE_COUNT
        for i, (r, g, b) in enumerate(self._bar_colors):
            x = margin + i * (seg_w + gap)
            # A soft vertical falloff reads more like a diffused light bar than
            # a flat rectangle would.
            gradient = QLinearGradient(0, top, 0, top + bar_h)
            gradient.setColorAt(0.0, QColor(r, g, b).lighter(130))
            gradient.setColorAt(0.5, QColor(r, g, b))
            gradient.setColorAt(1.0, QColor(r // 2, g // 2, b // 2))
            painter.setBrush(gradient)
            painter.setPen(CHASSIS_LINE)
            painter.drawRoundedRect(QRectF(x, top, seg_w, bar_h), 7, 7)

        painter.setPen(CHASSIS_TEXT)
        for i in range(ZONE_COUNT):
            x = margin + i * (seg_w + gap)
            painter.drawText(int(x), self.height() - label_h - 6, int(seg_w), label_h,
                             Qt.AlignmentFlag.AlignCenter, f"zona {i + 1}")


# -- the effect form ----------------------------------------------------------------------

class EffectEditor(QWidget):
    """The parameter form for one effect."""

    changed = pyqtSignal()

    def __init__(self, base_mode: bool = False, allow_layout: bool = True,
                 zone_mode: bool = False):
        super().__init__()
        self._spec: dict = dict(DEFAULTS)
        self._extra: dict = {}      # keys we do not edit but must not lose
        self._loading = False
        # A default profile has nowhere to be queued and nothing to preempt, so
        # priority and target make no sense there and are hidden below.
        self.base_mode = base_mode
        # A zone inside a layout: it has no life of its own to end, and its
        # place in the stack is its priority.
        self.zone_mode = zone_mode
        # Neither a default profile nor a zone ever finishes, so "how many
        # repetitions" and "how long in total" have no answer here -- they
        # belong to the alert that plays the thing. What is left is a rate,
        # and the only rate that means anything without an end is cycles per
        # second, which is what the speed control becomes.
        self.endless = base_mode or zone_mode
        # knob name -> (label, field); used to hide what the chosen kind ignores
        self._rows: dict = {}

        # A zone inside a layout may not itself be a layout, so the picker is
        # left out there rather than offered and then rejected.
        self.allow_layout = allow_layout
        self._layout_registry: dict = {}
        # The layout named by the spec last loaded. Kept so that a picker not
        # yet populated -- or one missing a layout that was since deleted --
        # cannot silently turn a layout profile into a plain effect.
        self._loaded_layout = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # -- zone layout ---------------------------------------------------
        self.layout_box = Section("Zone")
        form = self.layout_box.form
        self.use_layout = Switch(
            "Usa un layout a zone",
            "Invece di un solo effetto su tutta la superficie applica un layout:\n"
            "ogni zona ha il suo effetto. I layout si creano nella scheda Zone.")
        self.use_layout.toggled.connect(self._touch)
        form.addRow(self.use_layout)
        self.layout_pick = QComboBox()
        self.layout_pick.setMinimumWidth(200)
        self.layout_pick.currentTextChanged.connect(self._touch)
        form.addRow("Layout", self.layout_pick)
        self.layout_hint = note()
        form.addRow(self.layout_hint)
        self.layout_box.setVisible(allow_layout)
        layout.addWidget(self.layout_box)

        # -- colour --------------------------------------------------------
        look = Section("Aspetto")
        form = look.form
        self.kind = QComboBox()
        self.kind.addItems(fx.CANONICAL)
        self.kind.setMinimumWidth(200)
        self.kind.currentTextChanged.connect(self._touch)
        form.addRow("Tipo", self.kind)
        self.kind_label = form.labelForField(self.kind)
        self.colors = ColorList()
        self.colors.changed.connect(self._touch)
        form.addRow("Colori", self.colors)
        self.colors_row_label = form.labelForField(self.colors)
        self.gradient_bar = GradientBar()
        self.gradient_bar.changed.connect(self._touch)
        self.gradient_row_label = QLabel("Distribuzione")
        form.addRow(self.gradient_row_label, self.gradient_bar)
        self.colors_hint = note()
        form.addRow("", self.colors_hint)

        self.brightness = QSpinBox()
        self.brightness.setRange(-1, 100)
        self.brightness.setSpecialValueText("eredita")
        self.brightness.setSuffix(" %")
        self.brightness.setToolTip("−1 = eredita il livello generale")
        self.brightness.valueChanged.connect(self._touch)
        form.addRow("Luminosità", self.brightness)
        layout.addWidget(look)

        # -- timing --------------------------------------------------------
        timing = Section("Tempi")
        form = timing.form

        self.auto_duration = Switch("dalla velocità")
        self.auto_duration.setChecked(True)
        self.auto_duration.toggled.connect(self._toggle_duration)
        self.duration = QSpinBox()
        self.duration.setRange(50, 20000)
        self.duration.setSingleStep(50)
        self.duration.setSuffix(" ms")
        self.duration.setValue(1400)
        self.duration.valueChanged.connect(self._touch)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(self.duration)
        row.addWidget(self.auto_duration)
        row.addStretch(1)
        self._add_row(form, "Durata", holder, "duration")

        self.speed = self._slider(1, 10, 7, form, "Velocità",
                                  "quanto in fretta il fronte attraversa una zona",
                                  key="speed")
        # A layout has no single rate, so its length can be said two ways.
        # Never both at once: giving a count *and* a total is what used to make
        # a duration silently stretch one cycle instead of repeating it.
        self.length_mode = QComboBox()
        self.length_mode.addItem("durata", "duration")
        self.length_mode.addItem("ripetizioni", "cycles")
        self.length_mode.setToolTip(
            "Quanto dura l'avviso. «ripetizioni» conta i cicli della zona\n"
            "più lenta del layout, l'unica misura che le zone hanno in comune\n"
            "quando ognuna va a un ritmo suo.")
        self.length_mode.currentIndexChanged.connect(self._touch)
        self._add_row(form, "Lunghezza", self.length_mode, "length_mode")

        self.repeats = QSpinBox()
        self.repeats.setRange(1, 99)
        self.repeats.setValue(1)
        self.repeats.setSuffix(" cicli della zona più lenta")
        self.repeats.valueChanged.connect(self._touch)
        self._add_row(form, "Ripetizioni", self.repeats, "repeats")

        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.1, 10.0)
        self.rate.setSingleStep(0.1)
        self.rate.setDecimals(1)
        self.rate.setValue(1.0)
        self.rate.setSuffix(" cicli/s")
        self.rate.setToolTip(
            "Quanti cicli completi l'effetto fa al secondo:\n"
            "un respiro, un lampeggio, un passaggio del fronte.\n"
            "0,5 = un ciclo ogni due secondi.")
        self.rate.valueChanged.connect(self._touch)
        self._add_row(form, "Cicli al secondo", self.rate, "rate")

        self.fade = QSpinBox()
        # -1 rather than a number, so an effect that has never been given a
        # value reads as "inherits" instead of showing a figure the user never
        # chose and then wondering why editing it changes nothing elsewhere.
        self.fade.setRange(-1, 15000)
        self.fade.setSingleStep(100)
        self.fade.setSuffix(" ms")
        self.fade.setSpecialValueText("eredita dal demone")
        self.fade.setValue(-1)
        self.fade.setToolTip(
            "Quanto dura la dissolvenza verso l'aspetto predefinito quando\n"
            "QUESTO avviso finisce: il valore è per avviso.\n"
            "  «eredita dal demone» usa daemon.fade_back_ms (300 ms di serie)\n"
            "  0 passa di colpo\n"
            "Per cambiarlo per tutti gli avvisi insieme modifica\n"
            "daemon.fade_back_ms in /etc/omen-fx/config.toml.")
        self.fade.valueChanged.connect(self._touch)
        self._add_row(form, "Ritorno", self.fade, "fade")
        self.timing_group = timing
        layout.addWidget(timing)

        # -- smoothness ----------------------------------------------------
        smooth = Section("Morbidezza")
        form = smooth.form
        self.smoothness = self._slider(
            0, 100, 45, form, "Spaziale",
            "quanto una zona si accende mentre il fronte è ancora su quella "
            "accanto: alzala per togliere il gradino da un LED al successivo",
            scale=100.0, key="smoothness")
        self.gamma = self._slider(
            100, 300, 220, form, "Naturalezza (gamma)",
            "corregge la risposta non lineare dell'occhio; senza, una "
            "dissolvenza sembra crollare in alto e strisciare in basso",
            scale=100.0, key="gamma")
        self.tail = self._slider(
            2, 95, 45, form, "Scia",
            "quanto una zona resta accesa mentre il fronte la lascia", scale=100.0,
            key="tail")
        self.smooth_group = smooth
        layout.addWidget(smooth)

        # -- misc ----------------------------------------------------------
        other = Section("Riproduzione")
        form = other.form
        self.priority = QSpinBox()
        self.priority.setRange(0, 100)
        self.priority.setToolTip("Un avviso con priorità più alta interrompe quelli più bassi.")
        self.priority.valueChanged.connect(self._touch)
        form.addRow("Priorità", self.priority)
        self.target = QComboBox()
        for key, text in TARGETS:
            self.target.addItem(text, key)
        self.target.setToolTip("dove gira l'effetto: la barra, la tastiera o entrambe")
        self.target.currentIndexChanged.connect(self._touch)
        form.addRow("Superficie", self.target)
        self.target_label = form.labelForField(self.target)
        if self.base_mode:
            # A default profile only has these two; with both hidden the
            # section has nothing left to show.
            other.hide()
        layout.addWidget(other)

        self.info = note()
        layout.addWidget(self.info)
        layout.addStretch()

    def _add_row(self, form, label: str, field, key: str) -> None:
        holder = QLabel(label)
        form.addRow(holder, field)
        self._rows[key] = (holder, field)

    def _slider(self, low, high, value, form, label, tip, scale=1.0, key=None):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        slider.setToolTip(tip)
        readout = W.label("", "muted")
        readout.setMinimumWidth(44)
        slider.valueChanged.connect(
            lambda v: readout.setText(f"{v / scale:g}" if scale != 1 else str(v)))
        slider.valueChanged.connect(self._touch)
        readout.setText(f"{value / scale:g}" if scale != 1 else str(value))
        field = QWidget()
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout(field)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(slider, 1)
        row.addWidget(readout)
        holder = QLabel(label)
        holder.setToolTip(tip)
        form.addRow(holder, field)
        if key:
            self._rows[key] = (holder, field)
        slider.scale = scale  # type: ignore[attr-defined]
        return slider

    def _target(self) -> str:
        return self.target.currentData() or "both"

    # -- state ------------------------------------------------------------

    def load(self, spec: dict) -> None:
        self._loading = True
        self._spec = dict(spec)
        # "colors" belongs here: left out, it fell through to _extra and was
        # re-emitted verbatim, so a palette from a previous kind kept
        # overriding the colour actually picked.
        known = {"kind", "color", "colors", "speed", "cycles", "smoothness",
                 "target", "gamma", "tail", "priority", "brightness",
                 "duration_ms", "color2", "stops", "fade_back_ms", "period_ms",
                 # A layout reference is rebuilt from the picker, and its zones
                 # belong to the layout file -- neither may survive in _extra.
                 "layout", "zones", "bar_source"}
        self._extra = {k: v for k, v in spec.items() if k not in known}

        is_layout = str(spec.get("kind", "")).lower() == lay.LAYOUT_KIND
        self._loaded_layout = str(spec.get("layout") or "") if is_layout else ""
        self.use_layout.setChecked(is_layout and self.allow_layout)
        if self._loaded_layout:
            self.layout_pick.setCurrentText(self._loaded_layout)
        self.kind.setCurrentText(
            fx.canonical(spec.get("kind", "center_out")) if not is_layout
            else self.kind.currentText())
        palette = spec.get("colors") or [spec.get("color", "00A8FF")]
        if str(spec.get("kind", "")).lower() == "gradient" and len(palette) < 2:
            palette = list(palette) + [spec.get("color2", "000000")]
        self.colors.set_kind(str(spec.get("kind", "center_out")))
        self.colors.set_colors(palette)
        self.gradient_bar.set_gradient(self.colors.colors(), spec.get("stops"))
        self.speed.setValue(int(spec.get("speed", 7)))
        # A profile stored under the old "speed" knob keeps its look: the rate
        # is read back off the effect it actually builds, not guessed, so
        # opening the editor cannot quietly change what the surface shows.
        period = spec.get("period_ms")
        if (not period and self.endless and spec.get("speed") is not None
                and str(spec.get("kind", "")).lower() != lay.LAYOUT_KIND):
            try:
                period = fx.build(spec).per_cycle * 1000.0
            except (ValueError, TypeError, KeyError):
                period = None
        rate = 1000.0 / float(period) if period else 1.0
        self.rate.setValue(round(min(10.0, max(0.1, rate)), 1))
        self.smoothness.setValue(round(float(spec.get("smoothness", 0.45)) * 100))
        self.target.setCurrentIndex(max(0, self.target.findData(str(spec.get("target", "both")))))
        self.gamma.setValue(round(float(spec.get("gamma", 2.2)) * 100))
        self.tail.setValue(round(float(spec.get("tail", 0.45)) * 100))
        self.priority.setValue(int(spec.get("priority", 50)))
        self.fade.setValue(int(spec.get("fade_back_ms", -1)))
        self.brightness.setValue(int(spec.get("brightness", -1)))
        # An alert tuned with a repetition count keeps its length: the total is
        # read off the effect it actually builds, so dropping the count from
        # the editor cannot quietly shorten or stretch what it already does.
        if (not self.endless and "duration_ms" not in spec
                and spec.get("cycles") not in (None, 0)):
            try:
                built = fx.build(spec).duration
            except (ValueError, TypeError, KeyError):
                built = None
            if built:
                spec = {**spec, "duration_ms": int(round(built * 1000))}
        by_cycles = ("duration_ms" not in spec
                     and str(spec.get("kind", "")).lower() == lay.LAYOUT_KIND
                     and spec.get("cycles") not in (None, 0))
        self.length_mode.setCurrentIndex(1 if by_cycles else 0)
        self.repeats.setValue(int(spec["cycles"]) if by_cycles else 1)
        has_duration = "duration_ms" in spec
        self.auto_duration.setChecked(not has_duration)
        if has_duration:
            self.duration.setValue(int(spec["duration_ms"]))
        self.duration.setEnabled(has_duration)
        self._loading = False
        self._touch()

    def spec(self) -> dict:
        """Only the parameters this kind actually reads.

        Emitting a hidden knob would be worse than useless: a leftover
        ``cycles`` on a ``solid`` would give it a duration and make it end,
        which is exactly the sort of ghost value that is impossible to explain
        from the editor.
        """
        if self._layout_mode:
            return self._layout_spec()
        kind = self.kind.currentText()
        knobs = fx.knobs_for(kind)
        chosen = self.colors.colors()
        out = dict(self._extra)
        out["kind"] = kind
        out["color"] = chosen[0]
        for key in ("speed", "cycles", "smoothness", "gamma", "tail"):
            out.pop(key, None)
        out.pop("period_ms", None)
        if "speed" in knobs:
            if self.endless:
                out["period_ms"] = int(round(1000.0 / self.rate.value()))
            else:
                out["speed"] = self.speed.value()
        if self.endless:
            # Explicit rather than omitted: 0 is what makes it endless, and
            # leaving it out would let an effect's own default end it.
            out["cycles"] = 0
        else:
            # Alerts say how long, never how many times: the effect keeps its
            # own rate and repeats as many times as fit. With a zone layout a
            # single count would be meaningless anyway, since zones run at
            # rates of their own.
            out.pop("cycles", None)
        if "smoothness" in knobs:
            out["smoothness"] = round(self.smoothness.value() / 100, 2)
        if "gamma" in knobs:
            out["gamma"] = round(self.gamma.value() / 100, 2)
        if "tail" in knobs:
            out["tail"] = round(self.tail.value() / 100, 2)

        if not self.base_mode:
            out["priority"] = self.priority.value()
            out["target"] = self._target()
            if self.fade.value() >= 0:
                out["fade_back_ms"] = self.fade.value()
            else:
                out.pop("fade_back_ms", None)
        else:
            for key in ("priority", "target", "fade_back_ms"):
                out.pop(key, None)

        # Emit the two colour keys coherently: a stale "colors" list would win
        # over "color" inside the effect and silently ignore the pick.
        if len(chosen) > 1:
            out["colors"] = chosen
        else:
            out.pop("colors", None)
        out.pop("color2", None)
        if "stops" in knobs and len(chosen) > 1:
            out["stops"] = self.gradient_bar.stops()
        else:
            out.pop("stops", None)

        if self.brightness.value() >= 0:
            out["brightness"] = self.brightness.value()
        else:
            out.pop("brightness", None)
        if ("duration" in knobs and not self.endless
                and not self.auto_duration.isChecked()):
            out["duration_ms"] = self.duration.value()
        else:
            out.pop("duration_ms", None)
        return out

    @property
    def _layout_name(self) -> str:
        return self.layout_pick.currentText() or self._loaded_layout

    @property
    def _layout_mode(self) -> bool:
        return (self.allow_layout and self.use_layout.isChecked()
                and bool(self._layout_name))

    def _layout_spec(self) -> dict:
        """A reference to a stored layout, plus the knobs that still apply.

        The zones themselves are deliberately not copied in: they live in
        layouts.toml, so editing a layout updates everything using it instead
        of leaving stale copies behind in each effect.
        """
        out = dict(self._extra)
        out["kind"] = lay.LAYOUT_KIND
        out["layout"] = self._layout_name
        for key in ("color", "colors", "stops", "speed", "cycles", "smoothness",
                    "gamma", "tail", "zones", "bar_source"):
            out.pop(key, None)
        if self.base_mode:
            for key in ("priority", "target", "fade_back_ms"):
                out.pop(key, None)
        else:
            out["priority"] = self.priority.value()
            # The target limits where the layout is played. In particular,
            # editing a bar-only notification must not claim the keyboard.
            out["target"] = self._target()
            if self.fade.value() >= 0:
                out["fade_back_ms"] = self.fade.value()
            else:
                out.pop("fade_back_ms", None)
        if self.brightness.value() >= 0:
            out["brightness"] = self.brightness.value()
        else:
            out.pop("brightness", None)
        # Zones have no end of their own any more -- they are rates, not runs
        # -- so the alert that plays a layout is the only thing that can say
        # when it stops. A default profile is meant to last forever and says
        # nothing.
        out.pop("cycles", None)
        out.pop("duration_ms", None)
        if not self.base_mode:
            if self.length_mode.currentData() == "cycles":
                out["cycles"] = self.repeats.value()
            else:
                out["duration_ms"] = self.duration.value()
        return out

    def set_layouts(self, names, registry: dict | None = None) -> None:
        """Refresh the pickable layouts, keeping the current choice if it survives."""
        keep = self.layout_pick.currentText()
        blocked = self._loading
        self._loading = True
        self.layout_pick.clear()
        self.layout_pick.addItems(list(names))
        if keep:
            self.layout_pick.setCurrentText(keep)
        self._loading = blocked
        self._layout_registry = dict(registry or {})
        self.use_layout.setEnabled(bool(names))
        self._update_layout_hint()

    def _update_layout_hint(self) -> None:
        if self.layout_pick.count() == 0:
            self.layout_hint.setText("Nessun layout: creane uno nella scheda Zone.")
            return
        if not self._layout_mode:
            self.layout_hint.setText("")
        elif self.base_mode:
            self.layout_hint.setText(
                "Le zone si modificano nella scheda Zone; qui scegli solo "
                "quale layout usare.")
        elif self.length_mode.currentData() == "cycles":
            self.layout_hint.setText(
                "Le zone girano all'infinito: l'avviso dura il numero di "
                "cicli qui sotto, contati sulla zona più lenta.")
        else:
            self.layout_hint.setText(
                "Le zone girano all'infinito, quindi è la durata qui sotto a "
                "decidere quanto dura l'avviso. Ogni zona ci gira dentro al "
                "suo ritmo.")

    def _row_visible(self, key, visible) -> None:
        row = self._rows.get(key)
        if row is not None:
            row[0].setVisible(visible)
            row[1].setVisible(visible)

    def _sync_layout(self) -> None:
        """In layout mode the whole-surface effect controls do not apply.

        Runs last in ``_touch`` so it overrides what the per-kind syncs just
        decided. Brightness is deliberately left alone: it scales whatever the
        layout draws, exactly as it scales a plain effect.
        """
        self.layout_pick.setVisible(self.allow_layout and self.use_layout.isChecked())
        lbl = self.layout_box.form.labelForField(self.layout_pick)
        if lbl is not None:
            lbl.setVisible(self.layout_pick.isVisibleTo(self.layout_box))
        if not self._layout_mode:
            self._update_layout_hint()
            self.auto_duration.setVisible(not self.endless)
            visible = not self.base_mode
            self.target.setVisible(visible)
            if self.target_label is not None:
                self.target_label.setVisible(visible)
            for key in ("length_mode", "repeats"):
                self._row_visible(key, False)
            for widget in (self.kind, self.kind_label):
                if widget is not None:
                    widget.setVisible(True)
            return
        for widget in (self.colors, self.gradient_bar, self.gradient_row_label,
                       self.colors_hint, self.kind, self.kind_label,
                       self.colors_row_label):
            if widget is not None:
                widget.setVisible(False)
        self.smooth_group.setVisible(False)
        self._update_layout_hint()

        # An alert says how long the layout runs, either way round; a default
        # profile runs until something else takes over and says neither.
        alert = not self.base_mode
        by_cycles = self.length_mode.currentData() == "cycles"
        for key, wanted in (("length_mode", alert),
                            ("repeats", alert and by_cycles),
                            ("duration", alert and not by_cycles)):
            self._row_visible(key, wanted)

        # Zones define the picture; the event can limit it to one surface.
        self.target.setVisible(alert)
        if self.target_label is not None:
            self.target_label.setVisible(alert)

        # "derive from the speed" has nothing to derive from: a layout has no
        # single speed, and its zones never end.
        self.auto_duration.setVisible(False)
        if self.auto_duration.isChecked():
            self.auto_duration.setChecked(False)
        self.duration.setEnabled(True)
        for key in ("speed", "cycles", "smoothness", "gamma", "tail"):
            self._row_visible(key, False)
        # A layout plays for as long as the alert says; without a timing row
        # left the whole section would be an empty caption.
        self.timing_group.setVisible(alert)

    def _toggle_duration(self, auto: bool) -> None:
        self.duration.setEnabled(not auto)
        self._touch()

    def _sync_knobs(self) -> None:
        """Show only the knobs this kind reads, and hide groups left empty."""
        knobs = fx.knobs_for(self.kind.currentText())
        shown = 0
        for key, (label, field) in self._rows.items():
            if key == "fade":
                # Nothing that never ends has anything to fade back to.
                visible = not self.endless
            elif key == "rate":
                visible = self.endless and "speed" in knobs
            elif key == "speed":
                visible = (not self.endless) and "speed" in knobs
            elif key == "duration":
                # Total length is the playing alert's business.
                visible = (not self.endless) and key in knobs
            else:
                visible = key in knobs
            label.setVisible(visible)
            field.setVisible(visible)
            if visible and key in ("fade", "rate", "speed", "duration"):
                shown += 1
        self.timing_group.setVisible(shown > 0)
        self.smooth_group.setVisible(
            bool(knobs & {"smoothness", "gamma", "tail"}))

    def _sync_colors(self) -> None:
        """Show only the colour controls this kind can actually use."""
        kind = self.kind.currentText().lower()
        self.colors.set_kind(kind)
        uses_colors = self.colors.PER_KIND.get(kind, (1, self.colors.MAXIMUM))[1] > 0
        self.colors.setVisible(uses_colors)
        if self.colors_row_label is not None:
            self.colors_row_label.setVisible(uses_colors)
        is_gradient = kind == "gradient"
        self.gradient_bar.setVisible(is_gradient)
        self.gradient_row_label.setVisible(is_gradient)
        self.colors_hint.setVisible(True)
        if is_gradient:
            # Keep the handles where they were when only a colour changed.
            self.gradient_bar.set_gradient(self.colors.colors(),
                                           self.gradient_bar.stops())

    def _update_hint(self) -> None:
        kind = self.kind.currentText().lower()
        count = len(self.colors.colors())
        if kind == "gradient":
            text = "i punti del gradiente, da sinistra a destra (dall'alto in basso con axis=v)"
        elif kind == "alternate":
            text = "i due colori si alternano a quarti"
        elif kind in ("wave", "rainbow"):
            text = "questo tipo genera da sé i colori dell'arcobaleno"
        elif count > 1:
            text = f"un colore per ripetizione, a ciclo ({count} in tutto)"
        elif self.colors.PER_KIND.get(kind, (1, 8))[1] > 1:
            text = "aggiungine altri con + per alternarli a ogni ripetizione"
        else:
            text = ""
        self.colors_hint.setText(text)

    def _touch(self, *_args) -> None:
        self._sync_knobs()
        self._sync_colors()
        self._update_hint()
        self._sync_layout()
        if not self._loading:
            self.changed.emit()

    def describe(self, effect, fast: bool) -> None:
        """Say what the effect will really do, hardware limits included."""
        if effect is None:
            self.info.setText("")
            return
        target = self._target()
        duration = getattr(effect, "duration", None)
        text = ("durata: illimitata (finché non viene rilasciato)"
                if duration is None else f"durata: {duration:.2f} s")

        # The keyboard firmware caps updates at 30 Hz; a bar write costs about
        # 30 ms on top, so driving both at once lands near 24 Hz.
        if target == "keys":
            text += " · ~30 fps"
        elif target == "bar":
            text += f" · ~{1000 / (FRAME_FLOOR_MS if fast else FRAME_FLOOR_SLOW_MS):.0f} fps"
        else:
            text += " · ~24 fps (tastiera e barra insieme)"
        if target != "keys" and not fast:
            text += ("\n▲ il driver non ha l'attributo «zones»: ogni fotogramma della "
                     "barra costa quattro chiamate WMI invece di una.")
        self.info.setText(text)


# -- the zone grid ------------------------------------------------------------------------

# Fallback colours, used only for the kinds that generate their own hues
# (rainbow, wave) and so have no single colour to show.
ZONE_TINTS = [
    "#4FA3FF", "#FF7A4F", "#5BD97E", "#C77DFF", "#FFD24F",
    "#4FE0D8", "#FF6FA5", "#9FE04F",
]

# Kinds whose colours come from the effect itself rather than from the spec.
GENERATED = ("rainbow", "wave")

# The grid's own frame, and the strip under it that holds the drag hint. Shared
# by _metrics and paintEvent so the two cannot disagree about where the strip
# begins -- they did, and the hint lost its descenders to the bottom edge.
MARGIN = 12
LABEL_H = 20


def tint(index: int) -> QColor:
    return QColor(ZONE_TINTS[index % len(ZONE_TINTS)])


def zone_color(zone: dict, index: int) -> QColor:
    """The colour that stands for a zone in the editor.

    Its own colour, not an arbitrary tint: the grid is a map of what the
    keyboard will look like, so a zone painted teal has to read as teal the
    moment you change it -- otherwise the editor and the hardware disagree and
    only the hardware is right.
    """
    if str(zone.get("kind", "")).lower() in GENERATED:
        return tint(index)
    palette = zone.get("colors") or []
    text = str(palette[0] if palette else zone.get("color", "")).strip()
    if not text:
        return tint(index)
    color = QColor("#" + text.lstrip("#"))
    return color if color.isValid() else tint(index)


class ZoneGrid(QWidget):
    """The paintable map of both surfaces.

    Drag with the left button to add cells to the selected zone, with the right
    button (or holding Ctrl) to take them away.
    """

    changed = pyqtSignal()
    GAP = 2

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(240)
        self.setMouseTracking(False)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._zones: list[dict] = []
        self._selected = -1
        self._painting = 0        # +1 adding, -1 erasing, 0 idle
        self._frame_keys: list = []
        self._frame_bar: list = []

    def set_zones(self, zones: list[dict], selected: int) -> None:
        self._zones = zones
        self._selected = selected
        self.update()

    def set_frame(self, keys, bar) -> None:
        """Show the composed output instead of the zone map."""
        self._frame_keys, self._frame_bar = keys, bar
        self.update()

    def _metrics(self):
        margin = MARGIN
        usable = self.height() - 2 * margin - LABEL_H - 10
        keys_h = usable * 0.70
        bar_h = usable - keys_h
        width = self.width() - 2 * margin
        return (margin, width, keys_h, bar_h,
                width / lay.GRID_COLS, keys_h / lay.GRID_ROWS)

    def _cell_at(self, pos):
        """Which cell a point falls in: ("keys", r, c), ("bar", 0, i) or None."""
        margin, width, keys_h, bar_h, cw, ch = self._metrics()
        x, y = pos.x() - margin, pos.y() - margin
        if 0 <= x < width and 0 <= y < keys_h:
            return ("keys", int(y // ch), int(x // cw))
        top = keys_h + 10
        if 0 <= x < width and top <= y < top + bar_h:
            return ("bar", 0, min(lay.BAR_CELLS - 1,
                                  int(x // (width / lay.BAR_CELLS))))
        return None

    def _apply(self, pos) -> None:
        if self._selected < 0 or self._selected >= len(self._zones):
            return
        hit = self._cell_at(pos)
        if hit is None:
            return
        surface_, r, c = hit
        zone = self._zones[self._selected]
        if surface_ == "keys":
            cells = set(lay.parse_mask(zone.get("mask")))
            before = len(cells)
            cells.add((r, c)) if self._painting > 0 else cells.discard((r, c))
            if len(cells) == before:
                return
            zone["mask"] = lay.dump_mask(cells)
        else:
            segs = set(lay.parse_bar(zone.get("bar")))
            before = len(segs)
            segs.add(c) if self._painting > 0 else segs.discard(c)
            if len(segs) == before:
                return
            zone["bar"] = lay.dump_bar(segs)
        self.update()
        self.changed.emit()

    def mousePressEvent(self, event) -> None:
        erase = (event.button() == Qt.MouseButton.RightButton
                 or event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._painting = -1 if erase else 1
        self._apply(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._painting:
            self._apply(event.position())

    def mouseReleaseEvent(self, _event) -> None:
        self._painting = 0

    def _owner(self, surface_: str, r: int, c: int) -> int:
        """Index of the topmost zone claiming a cell, or -1.

        Topmost, not first: this is the same last-wins rule the renderer uses,
        so the editor cannot disagree with the hardware about who owns a cell.
        """
        found = -1
        for i, zone in enumerate(self._zones):
            if surface_ == "keys":
                if (r, c) in lay.parse_mask(zone.get("mask")):
                    found = i
            elif c in lay.parse_bar(zone.get("bar")):
                found = i
        return found

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(CHASSIS)
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)
        margin, width, keys_h, bar_h, cw, ch = self._metrics()

        for r in range(lay.GRID_ROWS):
            for c in range(lay.GRID_COLS):
                owner = self._owner("keys", r, c)
                self._draw_cell(painter, margin + c * cw, margin + r * ch,
                                cw, ch, owner, r * lay.GRID_COLS + c,
                                self._frame_keys, 3)

        top = margin + keys_h + 10
        seg_w = width / lay.BAR_CELLS
        for i in range(lay.BAR_CELLS):
            owner = self._owner("bar", 0, i)
            self._draw_cell(painter, margin + i * seg_w, top, seg_w, bar_h,
                            owner, i, self._frame_bar, 6)

        painter.setPen(CHASSIS_TEXT)
        # Inside the band _metrics reserved for it, not flush against the
        # bottom edge -- drawn at height-13 the descenders were clipped off.
        painter.drawText(margin, int(self.height() - MARGIN - LABEL_H + 4),
                         int(width), LABEL_H, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         "trascina per assegnare · tasto destro o Ctrl per togliere")

    def _draw_cell(self, painter, x, y, w, h, owner, index, frame, radius) -> None:
        selected = owner == self._selected and owner >= 0
        # The composed output, always: the grid is what the hardware will show.
        # The zone's own colour stands in only until the first frame arrives.
        if index < len(frame) and frame[index] is not None:
            r, g, b = frame[index]
            body = QColor(r, g, b)
        elif owner < 0:
            body = QColor(30, 30, 34)          # claimed by nobody: transparent
        else:
            body = zone_color(self._zones[owner], owner)
        painter.setBrush(body)
        if selected:
            # The selection is carried by the outline alone, so the fill can
            # stay truthful even for a very dark zone colour.
            pen = QColor(255, 255, 255)
        elif owner >= 0:
            pen = body.lighter(160)
        else:
            pen = CHASSIS_LINE
        painter.setPen(pen)
        painter.drawRoundedRect(QRectF(x + self.GAP, y + self.GAP,
                                       w - 2 * self.GAP, h - 2 * self.GAP), radius, radius)
