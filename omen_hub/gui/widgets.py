"""Building blocks: cards, stat tiles, status pills, segmented controls,
live line charts, a ring gauge and the fan-curve editor."""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

from PyQt6.QtCore import (QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool,
                          pyqtSignal)
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QVBoxLayout, QWidget, QAbstractButton)

from .style import Tokens, mix, number_font

T: Tokens | None = None      # set by the app before any widget is built


# -- threads -----------------------------------------------------------------------

class _Signals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class _Job(QRunnable):
    def __init__(self, fn, sig):
        super().__init__()
        self.fn, self.sig = fn, sig

    def run(self):
        try:
            res = self.fn()
        except Exception as exc:          # reported to the GUI, never raised in a thread
            self.sig.failed.emit(str(exc))
        else:
            self.sig.done.emit(res)


_live: set = set()


def run_async(fn: Callable, on_done=None, on_fail=None):
    sig = _Signals()
    _live.add(sig)

    def finish(*_):
        _live.discard(sig)
    if on_done:
        sig.done.connect(on_done)
    if on_fail:
        sig.failed.connect(on_fail)
    sig.done.connect(finish)
    sig.failed.connect(finish)
    QThreadPool.globalInstance().start(_Job(fn, sig))


# -- layout pieces --------------------------------------------------------------------

def label(text="", obj=None, wrap=False, size=None, bold=False) -> QLabel:
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    if wrap:
        lab.setWordWrap(True)
    if size or bold:
        f = lab.font()
        if size:
            f.setPointSizeF(size)
        if bold:
            f.setWeight(QFont.Weight.DemiBold)
        lab.setFont(f)
    return lab


class Card(QFrame):
    def __init__(self, title: str | None = None, subtitle: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 16, 18, 16)
        self.lay.setSpacing(10)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        if title:
            col = QVBoxLayout()
            col.setSpacing(2)
            self.title = label(title, "cardTitle")
            col.addWidget(self.title)
            self.subtitle = label(subtitle or "", "cardSub", wrap=True)
            col.addWidget(self.subtitle)
            self.header.addLayout(col, 1)
            self.lay.addLayout(self.header)

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.lay.addWidget(w, stretch)
        else:
            self.lay.addLayout(w, stretch)
        return w


class StatTile(QWidget):
    """Label on top, big number, optional sub-line."""

    def __init__(self, caption: str, unit: str = "", size: float = 22, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.cap = label(caption, "statLabel")
        row = QHBoxLayout()
        row.setSpacing(4)
        self.val = QLabel("–")
        self.val.setFont(number_font(size))
        self.unit = label(unit, "muted")
        row.addWidget(self.val, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        self.sub = label("", "faint")
        lay.addWidget(self.cap)
        lay.addLayout(row)
        lay.addWidget(self.sub)

    def set(self, value, sub: str | None = None, fmt="{:.0f}"):
        self.val.setText("–" if value is None else (fmt.format(value) if not isinstance(value, str) else value))
        if sub is not None:
            self.sub.setText(sub)


class Pill(QLabel):
    """Status with icon + word: never colour alone."""
    STYLES = {
        "ok": ("ok", "● attivo"), "warn": ("warn", "▲ parziale"), "failed": ("err", "✕ errore"),
        "off": ("off", "○ spento"), "missing": ("off", "– non installato"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set("off")

    def set(self, state: str, text: str | None = None):
        tok, default = self.STYLES.get(state, ("off", state))
        c = getattr(T, tok)
        bg = mix(T.surface, c, 0.16)
        self.setText(text or default)
        self.setStyleSheet(f"color: {c.name()}; background: {bg.name()}; border-radius: 9px;"
                           f"padding: 2px 10px; font-size: 9pt; font-weight: 600;")


class Segmented(QWidget):
    changed = pyqtSignal(str)

    def __init__(self, options: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        n = len(options)
        for i, (key, text) in enumerate(options):
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            r = ""
            if i == 0:
                r += "border-top-left-radius: 8px; border-bottom-left-radius: 8px;"
            if i == n - 1:
                r += "border-top-right-radius: 8px; border-bottom-right-radius: 8px;"
            if i > 0:
                r += "border-left: none;"
            if r:
                b.setStyleSheet(r)
            self.group.addButton(b)
            self.buttons[key] = b
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            lay.addWidget(b)
        lay.addStretch(1)

    def set(self, key: str | None):
        for k, b in self.buttons.items():
            b.blockSignals(True)
            b.setChecked(k == key)
            b.blockSignals(False)

    def value(self) -> str | None:
        return next((k for k, b in self.buttons.items() if b.isChecked()), None)


class Toggle(QAbstractButton):
    """iOS-style switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(42, 24)

    def sizeHint(self):
        return QSize(42, 24)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track = T.accent if on else T.surface2
        if not self.isEnabled():
            track = mix(track, T.bg, 0.5)
        p.setPen(QPen(T.border, 1))
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0.5, 0.5, 41, 23), 11.5, 11.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff") if on else T.muted)
        x = 21 if on else 3
        p.drawEllipse(QRectF(x, 3, 18, 18))


class Switch(QWidget):
    """The hub's toggle with its caption: a drop-in for a QCheckBox."""
    toggled = pyqtSignal(bool)

    def __init__(self, text: str, tip: str = "", parent=None, wrap: bool = False):
        super().__init__(parent)
        lay_ = QHBoxLayout(self)
        lay_.setContentsMargins(0, 2, 0, 2)
        lay_.setSpacing(10)
        self.toggle = Toggle()
        self.toggle.toggled.connect(self.toggled.emit)
        self.text = QLabel(text)
        self.text.setWordWrap(wrap)
        lay_.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        lay_.addWidget(self.text, 1)
        if tip:
            self.setToolTip(tip)

    def mouseReleaseEvent(self, e):        # the caption is clickable too
        if self.isEnabled() and e.button() == Qt.MouseButton.LeftButton:
            self.toggle.toggle()

    def isChecked(self) -> bool:
        return self.toggle.isChecked()

    def setChecked(self, on: bool):
        self.toggle.setChecked(bool(on))

    def setText(self, text: str):
        self.text.setText(text)


class SliderField(QWidget):
    """Slider between the real minimum and maximum, the value next to it and
    a reset button. ``None`` means "default": the slider rests on the default
    and the value reads «predefinito»."""
    changed = pyqtSignal()

    def __init__(self, lo: int, hi: int, default: int | None, fmt: Callable[[int], str],
                 step: int = 1, parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QSlider, QToolButton
        self.fmt, self.step = fmt, max(1, step)
        self._value: int | None = None
        self.default = default
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.lo_lab = label("", "faint")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimumWidth(200)
        self.hi_lab = label("", "faint")
        # fixed widths so sliders line up from one row to the next
        self.lo_lab.setFixedWidth(100)
        self.lo_lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.hi_lab.setFixedWidth(100)
        self.val = QLabel()
        self.val.setFixedWidth(170)
        self.reset = QToolButton()
        self.reset.setText("↺")
        self.reset.setToolTip("Torna al valore predefinito")
        self.reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset.setStyleSheet("QToolButton { border: none; font-size: 13pt; padding: 0 4px; }"
                                 "QToolButton:disabled { color: transparent; }")
        self.reset.clicked.connect(lambda: self.set_value(None, emit=True))
        for w in (self.lo_lab, self.slider, self.hi_lab, self.val, self.reset):
            lay.addWidget(w, 1 if w is self.slider else 0)
        self.slider.valueChanged.connect(self._moved)
        self.set_range(lo, hi, default)

    def set_range(self, lo: int, hi: int, default: int | None = None):
        self.lo, self.hi = int(lo), int(max(lo, hi))
        if default is not None:
            self.default = default
        self.slider.blockSignals(True)
        self.slider.setRange(0, (self.hi - self.lo) // self.step)
        self.slider.setPageStep(max(1, self.slider.maximum() // 10))
        self.slider.blockSignals(False)
        self.lo_lab.setText(self.fmt(self.lo))
        self.hi_lab.setText(self.fmt(self.hi))
        self.set_value(self._value)

    def _to_pos(self, v):
        return (max(self.lo, min(self.hi, v)) - self.lo) // self.step

    def _moved(self, pos):
        self._value = self.lo + pos * self.step
        self._show()
        self.changed.emit()

    def set_value(self, v: int | None, emit: bool = False):
        self._value = None if v is None else int(v)
        rest = self._value if self._value is not None else (self.default if self.default is not None else self.hi)
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_pos(rest))
        self.slider.blockSignals(False)
        self._show()
        if emit:
            self.changed.emit()

    def value(self) -> int | None:
        return self._value

    def _show(self):
        if self._value is None:
            d = f" ({self.fmt(self.default)})" if self.default is not None else ""
            self.val.setText(f"predefinito{d}")
            self.val.setStyleSheet(f"color: {T.faint.name()};")
            self.reset.setEnabled(False)
        else:
            self.val.setText(self.fmt(self._value))
            self.val.setStyleSheet(f"color: {T.text.name()}; font-weight: 600;")
            self.reset.setEnabled(True)


def hrow(*widgets, spacing=10, stretch_last=False) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(spacing)
    for w in widgets:
        if w is None:
            row.addStretch(1)
        elif isinstance(w, QWidget):
            row.addWidget(w)
        else:
            row.addLayout(w)
    if stretch_last:
        row.addStretch(1)
    return row


# -- charts -----------------------------------------------------------------------------

def nice_ticks(lo: float, hi: float, target: int = 4) -> list[float]:
    """Round axis ticks (1/2/2.5/5 × 10^n) covering [lo, hi]."""
    import math
    span = max(hi - lo, 1e-9)
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v < hi + step * 0.999:
        ticks.append(round(v, 6))
        v += step
    return ticks


class LineChart(QWidget):
    """Live time series. One y-axis per chart; legend chips carry the live
    values; hovering shows a crosshair and the values at that instant."""

    def __init__(self, series: list[str], unit: str, window_s: int = 300,
                 y_range: tuple[float, float] | None = None, y_floor: float | None = 0,
                 fmt="{:.0f}", height=150, parent=None):
        super().__init__(parent)
        self.names = series
        self.unit, self.window, self.fmt = unit, window_s, fmt
        self.y_range, self.y_floor = y_range, y_floor
        self.data = [deque() for _ in series]
        self.hover_x: float | None = None
        self.setMinimumHeight(height)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def push(self, t: float, values: list[float | None]):
        for d, v in zip(self.data, values):
            d.append((t, v))
            while d and d[0][0] < t - self.window:
                d.popleft()
        self.update()

    def clear(self):
        for d in self.data:
            d.clear()
        self.update()

    def leaveEvent(self, _):
        self.hover_x = None
        self.update()

    def mouseMoveEvent(self, e):
        self.hover_x = e.position().x()
        self.update()

    def _bounds(self):
        vals = [v for d in self.data for _, v in d if v is not None]
        if self.y_range:
            lo, hi = self.y_range
            if vals:
                hi = max(hi, max(vals))
            return lo, hi
        if not vals:
            return 0, 1
        lo, hi = min(vals), max(vals)
        pad = max((hi - lo) * 0.15, 1)
        lo = lo - pad if self.y_floor is None else max(self.y_floor, lo - pad)
        return lo, hi + pad

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        fm = p.fontMetrics()
        legend_h = fm.height() + 8
        left, right, top, bottom = 44, 12, legend_h + 4, fm.height() + 6
        plot = QRectF(left, top, max(10, w - left - right), max(10, h - top - bottom))
        lo, hi = self._bounds()
        ticks = nice_ticks(lo, hi)
        lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
        now = time.time()
        t0 = now - self.window

        def X(t):
            return plot.left() + (t - t0) / self.window * plot.width()

        def Y(v):
            return plot.bottom() - (v - lo) / (hi - lo or 1) * plot.height()

        # legend (also the live readout)
        x = 0.0
        for i, name in enumerate(self.names):
            last = next((v for _, v in reversed(self.data[i]) if v is not None), None)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.series[i])
            p.drawRoundedRect(QRectF(x, (legend_h - 10) / 2, 10, 10), 2, 2)
            txt = f"{name}  " + ("–" if last is None else self.fmt.format(last) + f" {self.unit}")
            p.setPen(T.text)
            p.drawText(QPointF(x + 16, legend_h / 2 + fm.ascent() / 2 - 1), txt)
            x += 16 + fm.horizontalAdvance(txt) + 22

        # grid + y labels (recessive)
        p.setPen(QPen(T.grid, 1))
        for v in ticks:
            y = Y(v)
            p.setPen(QPen(T.grid, 1))
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(T.faint)
            p.drawText(QRectF(0, y - fm.height() / 2, left - 8, fm.height()),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self.fmt.format(v))
        p.setPen(T.faint)
        p.drawText(QRectF(plot.left(), plot.bottom() + 4, 100, fm.height()),
                   Qt.AlignmentFlag.AlignLeft, f"−{self.window // 60} min")
        p.drawText(QRectF(plot.right() - 100, plot.bottom() + 4, 100, fm.height()),
                   Qt.AlignmentFlag.AlignRight, "ora")

        # lines, 2px, last point marker with a surface ring
        p.setClipRect(plot.adjusted(-6, -6, 6, 6))
        for i, d in enumerate(self.data):
            path, started = QPainterPath(), False
            for t, v in d:
                if v is None:
                    started = False
                    continue
                pt = QPointF(X(t), Y(v))
                if started:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    started = True
            col = T.series[i]
            p.setPen(QPen(col, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            last = next(((t, v) for t, v in reversed(d) if v is not None), None)
            if last:
                p.setPen(QPen(T.surface, 2))
                p.setBrush(col)
                p.drawEllipse(QPointF(X(last[0]), Y(last[1])), 4.5, 4.5)
        p.setClipping(False)

        # hover crosshair + tooltip
        if self.hover_x is not None and plot.left() <= self.hover_x <= plot.right():
            th = t0 + (self.hover_x - plot.left()) / plot.width() * self.window
            rows = []
            for i, d in enumerate(self.data):
                if not d:
                    continue
                t, v = min(d, key=lambda tv: abs(tv[0] - th))
                if abs(t - th) < 6 and v is not None:
                    rows.append((i, v))
            if rows:
                p.setPen(QPen(T.faint, 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(self.hover_x, plot.top()), QPointF(self.hover_x, plot.bottom()))
                ago = now - th
                lines = [f"{int(ago // 60)}:{int(ago % 60):02d} fa"] + \
                    [f"{self.names[i]}: {self.fmt.format(v)} {self.unit}" for i, v in rows]
                bw = max(fm.horizontalAdvance(s) for s in lines) + 30
                bh = len(lines) * (fm.height() + 2) + 10
                bx = self.hover_x + 12 if self.hover_x + 12 + bw < w else self.hover_x - 12 - bw
                box = QRectF(bx, plot.top() + 4, bw, bh)
                p.setPen(QPen(T.border, 1))
                p.setBrush(T.surface2)
                p.drawRoundedRect(box, 8, 8)
                yy = box.top() + 6 + fm.ascent()
                p.setPen(T.muted)
                p.drawText(QPointF(box.left() + 10, yy), lines[0])
                for (i, v), s in zip(rows, lines[1:]):
                    yy += fm.height() + 2
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(T.series[i])
                    p.drawRoundedRect(QRectF(box.left() + 10, yy - fm.ascent() + 2, 8, 8), 2, 2)
                    p.setPen(T.text)
                    p.drawText(QPointF(box.left() + 24, yy), s)
                    p.setBrush(T.series[i])
                    p.setPen(QPen(T.surface, 2))
                    p.drawEllipse(QPointF(self.hover_x, Y(v)), 4.5, 4.5)


class RingGauge(QWidget):
    def __init__(self, caption: str, unit: str, lo: float, hi: float, size=132, parent=None):
        super().__init__(parent)
        self.caption, self.unit, self.lo, self.hi = caption, unit, lo, hi
        self.value: float | None = None
        self.sub = ""
        self.color: QColor | None = None
        self.setFixedSize(size, size)

    def set(self, value, sub="", color: QColor | None = None):
        self.value, self.sub, self.color = value, sub, color
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(self.width(), self.height())
        r = QRectF(9, 9, s - 18, s - 18)
        p.setPen(QPen(T.surface2, 9, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(r, 225 * 16, -270 * 16)
        if self.value is not None:
            frac = max(0.0, min(1.0, (self.value - self.lo) / (self.hi - self.lo)))
            p.setPen(QPen(self.color or T.accent, 9, cap=Qt.PenCapStyle.RoundCap))
            p.drawArc(r, 225 * 16, int(-270 * 16 * frac))
        p.setPen(T.text)
        p.setFont(number_font(s / 6.2))
        txt = "–" if self.value is None else f"{self.value:.0f}"
        p.drawText(QRectF(0, s * 0.27, s, s * 0.3), Qt.AlignmentFlag.AlignCenter, txt)
        f = QFont(self.font())
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(T.muted)
        p.drawText(QRectF(0, s * 0.55, s, s * 0.14), Qt.AlignmentFlag.AlignCenter, self.unit)
        p.drawText(QRectF(0, s * 0.78, s, s * 0.16), Qt.AlignmentFlag.AlignCenter, self.caption)
        if self.sub:
            p.setPen(T.faint)
            p.drawText(QRectF(0, s * 0.66, s, s * 0.13), Qt.AlignmentFlag.AlignCenter, self.sub)


# -- fan curve editor ------------------------------------------------------------------

class CurveEditor(QWidget):
    """Drag points; double-click adds one, right-click removes it.
    Shows where the fans are right now (control temperature → %)."""
    changed = pyqtSignal()
    T_MIN, T_MAX = 30.0, 105.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[list[float]] = []
        self.drag: int | None = None
        self.hover: int | None = None
        self.marker: tuple[float, float] | None = None
        self.smooth = True
        self.setMinimumHeight(260)
        self.setMouseTracking(True)

    def set_points(self, pts):
        self.points = sorted([[float(t), float(v)] for t, v in pts])
        self.update()

    def set_marker(self, temp, pct):
        self.marker = None if temp is None else (temp, pct)
        self.update()

    def _plot(self):
        return QRectF(46, 14, max(10, self.width() - 62), max(10, self.height() - 44))

    def _to_px(self, t, v):
        r = self._plot()
        return QPointF(r.left() + (t - self.T_MIN) / (self.T_MAX - self.T_MIN) * r.width(),
                       r.bottom() - v / 100 * r.height())

    def _from_px(self, pos):
        r = self._plot()
        t = self.T_MIN + (pos.x() - r.left()) / r.width() * (self.T_MAX - self.T_MIN)
        v = (r.bottom() - pos.y()) / r.height() * 100
        return max(self.T_MIN, min(self.T_MAX, t)), max(0.0, min(100.0, v))

    def _hit(self, pos):
        for i, (t, v) in enumerate(self.points):
            if (self._to_px(t, v) - pos).manhattanLength() < 14:
                return i
        return None

    def mousePressEvent(self, e):
        i = self._hit(e.position())
        if e.button() == Qt.MouseButton.RightButton and i is not None and len(self.points) > 2:
            del self.points[i]
            self.changed.emit()
            self.update()
        elif e.button() == Qt.MouseButton.LeftButton:
            self.drag = i

    def mouseDoubleClickEvent(self, e):
        if self._hit(e.position()) is None:
            t, v = self._from_px(e.position())
            self.points.append([round(t, 1), round(v, 1)])
            self.points.sort()
            self.changed.emit()
            self.update()

    def mouseMoveEvent(self, e):
        if self.drag is not None:
            t, v = self._from_px(e.position())
            lo = self.points[self.drag - 1][0] + 1 if self.drag > 0 else self.T_MIN
            hi = self.points[self.drag + 1][0] - 1 if self.drag < len(self.points) - 1 else self.T_MAX
            self.points[self.drag] = [round(max(lo, min(hi, t)), 1), round(v, 1)]
            self.changed.emit()
        else:
            self.hover = self._hit(e.position())
        self.update()

    def mouseReleaseEvent(self, _):
        self.drag = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._plot()
        fm = p.fontMetrics()
        for v in range(0, 101, 25):
            y = self._to_px(self.T_MIN, v).y()
            p.setPen(QPen(T.grid, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(T.faint)
            p.drawText(QRectF(0, y - 8, r.left() - 8, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{v}%")
        for t in range(30, 106, 15):
            x = self._to_px(t, 0).x()
            p.setPen(QPen(T.grid, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(T.faint)
            p.drawText(QRectF(x - 20, r.bottom() + 6, 40, fm.height()),
                       Qt.AlignmentFlag.AlignHCenter, f"{t}°")
        if not self.points:
            return
        pts = [self._to_px(t, v) for t, v in self.points]
        path = QPainterPath(self._to_px(self.T_MIN, self.points[0][1]))
        prev = None
        for pt in pts:
            if not self.smooth and prev is not None:
                path.lineTo(QPointF(pt.x(), prev.y()))
            path.lineTo(pt)
            prev = pt
        path.lineTo(self._to_px(self.T_MAX, self.points[-1][1]))
        fill = QPainterPath(path)
        fill.lineTo(r.bottomRight())
        fill.lineTo(QPointF(r.left(), r.bottom()))
        fill.closeSubpath()
        c = QColor(T.accent)
        c.setAlpha(40)
        p.fillPath(fill, c)
        p.setPen(QPen(T.accent, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        for i, pt in enumerate(pts):
            p.setPen(QPen(T.surface, 2))
            p.setBrush(T.text if i in (self.hover, self.drag) else T.accent)
            p.drawEllipse(pt, 6, 6)
        if self.drag is not None or self.hover is not None:
            i = self.drag if self.drag is not None else self.hover
            t, v = self.points[i]
            p.setPen(T.text)
            pt = pts[i]
            p.drawText(QPointF(pt.x() + 10, pt.y() - 10), f"{t:.0f} °C → {v:.0f} %")
        if self.marker:
            t, v = self.marker
            pt = self._to_px(max(self.T_MIN, min(self.T_MAX, t)), v)
            p.setPen(QPen(T.series[1], 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(pt.x(), r.top()), QPointF(pt.x(), r.bottom()))
            p.setPen(QPen(T.surface, 2))
            p.setBrush(T.series[1])
            p.drawEllipse(pt, 5, 5)
            p.setPen(T.text)
            p.drawText(QPointF(min(pt.x() + 8, r.right() - 110), r.top() + 14),
                       f"ora {t:.0f} °C → {v:.0f} %")
