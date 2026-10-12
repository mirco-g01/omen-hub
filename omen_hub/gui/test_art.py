"""Hand-drawn, animated illustrations for the guided tests.

One widget, one scene per action (unplug the charger, plug the HDMI, hands
off, fans, CPU, GPU, sleep, screen off, wake, done...). A ring around the
scene shows the countdown. While an idle measurement runs the animation is
frozen: repainting at 30 fps would show up in the very numbers being taken.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import widgets as W
from .style import mix


class ActionArt(QWidget):
    FPS = 30

    def __init__(self, size: int = 240, parent=None):
        super().__init__(parent)
        self.setMinimumSize(size, size)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.kind = "idle"
        self.phase = 0.0            # seconds since the scene started
        self.progress: float | None = None      # 0..1 for the countdown ring
        self.center_text = ""
        self.speed = 1.0            # fan speed etc.
        self.timer = QTimer(self, interval=int(1000 / self.FPS), timeout=self._tick)

    # control ----------------------------------------------------------------------
    def set_scene(self, kind: str, animate: bool = True, speed: float = 1.0):
        self.kind, self.phase, self.speed = kind, 0.0, speed
        if animate:
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def freeze(self):
        self.timer.stop()
        self.update()

    def set_progress(self, frac: float | None, text: str = ""):
        self.progress = None if frac is None else max(0.0, min(1.0, frac))
        self.center_text = text
        self.update()

    def _tick(self):
        self.phase += 1 / self.FPS
        self.update()

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    # painting ---------------------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        T = W.T
        side = min(self.width(), self.height())
        box = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        # soft disc behind the scene
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(mix(T.surface, T.accent, 0.10))
        inner = box.adjusted(14, 14, -14, -14)
        p.drawEllipse(inner)
        # countdown ring
        ring = box.adjusted(5, 5, -5, -5)
        p.setPen(QPen(T.surface2, 7, cap=Qt.PenCapStyle.RoundCap))
        p.drawArc(ring, 0, 360 * 16)
        if self.progress is not None:
            p.setPen(QPen(T.accent, 7, cap=Qt.PenCapStyle.RoundCap))
            p.drawArc(ring, 90 * 16, -int(360 * 16 * self.progress))
        p.save()
        clip = QPainterPath()
        clip.addEllipse(inner)
        p.setClipPath(clip)
        p.translate(inner.center())
        s = inner.width() / 200.0          # scenes are drawn in a 200x200 box
        p.scale(s, s)
        getattr(self, f"_draw_{self.kind}", self._draw_idle)(p, T)
        p.restore()
        if self.center_text:
            p.setPen(T.text)
            f = QFont(self.font())
            f.setPointSizeF(11)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            p.drawText(QRectF(box.left(), box.bottom() - 46, box.width(), 22),
                       Qt.AlignmentFlag.AlignHCenter, self.center_text)

    # helpers ----------------------------------------------------------------------
    @staticmethod
    def _pen(c: QColor, w=4.0):
        return QPen(c, w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

    def _wave(self, period=1.6):
        return (math.sin(self.phase * 2 * math.pi / period) + 1) / 2

    def _arrow(self, p, x1, y, x2, color):
        p.setPen(self._pen(color, 5))
        p.drawLine(QPointF(x1, y), QPointF(x2, y))
        d = 1 if x2 > x1 else -1
        p.drawLine(QPointF(x2, y), QPointF(x2 - 10 * d, y - 9))
        p.drawLine(QPointF(x2, y), QPointF(x2 - 10 * d, y + 9))

    def _laptop_side(self, p, T, label):
        # the laptop edge with a port, on the left
        p.setPen(self._pen(T.muted, 3))
        p.setBrush(T.surface2)
        p.drawRoundedRect(QRectF(-92, -46, 46, 92), 8, 8)
        p.setBrush(T.bg)
        p.drawRoundedRect(QRectF(-58, -10, 12, 20), 3, 3)
        p.setPen(T.muted)
        f = QFont(self.font())
        f.setPointSizeF(7)
        p.setFont(f)
        p.drawText(QRectF(-96, 50, 56, 14), Qt.AlignmentFlag.AlignHCenter, label)

    def _plug(self, p, T, x, kind):
        """Connector whose tip sits at x, cable trailing to the right."""
        col = T.text
        p.setPen(self._pen(col, 3))
        p.setBrush(mix(T.surface, T.text, 0.15))
        if kind == "hdmi":
            tip = QPainterPath()
            tip.moveTo(x, -8)
            tip.lineTo(x + 14, -10)
            tip.lineTo(x + 14, 10)
            tip.lineTo(x, 8)
            tip.closeSubpath()
            p.drawPath(tip)
            p.drawRoundedRect(QRectF(x + 14, -16, 34, 32), 6, 6)
        else:
            p.drawRoundedRect(QRectF(x, -7, 14, 14), 3, 3)
            p.drawRoundedRect(QRectF(x + 14, -14, 30, 28), 7, 7)
        cable = QPainterPath(QPointF(x + 46, 0))
        cable.cubicTo(QPointF(x + 80, 0), QPointF(x + 70, 60), QPointF(x + 120, 60))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self._pen(col, 4))
        p.drawPath(cable)

    def _connector_scene(self, p, T, kind, unplug):
        self._laptop_side(p, T, "HDMI" if kind == "hdmi" else "alimentazione")
        t = (self.phase % 2.2) / 2.2
        ease = 1 - (1 - min(1.0, t * 1.4)) ** 3
        if unplug:
            x = -46 + 50 * ease
            self._plug(p, T, x, kind)
            if t > 0.25:
                self._arrow(p, 18, -40, 70, T.err)
        else:
            x = 4 - 50 * ease
            self._plug(p, T, x, kind)
            if t > 0.25:
                self._arrow(p, 70, -40, 18, T.ok)

    # scenes -------------------------------------------------------------------------
    def _draw_idle(self, p, T):
        p.setPen(self._pen(T.accent, 6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = 40 + 6 * self._wave()
        p.drawEllipse(QPointF(0, 0), r, r)

    def _draw_unplug_ac(self, p, T):
        self._connector_scene(p, T, "ac", True)

    def _draw_plug_ac(self, p, T):
        self._connector_scene(p, T, "ac", False)

    def _draw_unplug_hdmi(self, p, T):
        self._connector_scene(p, T, "hdmi", True)

    def _draw_plug_hdmi(self, p, T):
        self._connector_scene(p, T, "hdmi", False)

    def _draw_hands_off(self, p, T):
        # breathing rings + pause sign: "do not touch anything"
        for k in range(3):
            a = ((self.phase * 0.5 + k / 3) % 1.0)
            c = QColor(T.accent)
            c.setAlphaF(max(0.0, 0.55 * (1 - a)))
            p.setPen(self._pen(c, 3))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = 30 + 60 * a
            p.drawEllipse(QPointF(0, 0), r, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.accent)
        p.drawEllipse(QPointF(0, 0), 34, 34)
        p.setBrush(T.on_accent)
        p.drawRoundedRect(QRectF(-13, -16, 9, 32), 3, 3)
        p.drawRoundedRect(QRectF(4, -16, 9, 32), 3, 3)

    def _rotor(self, p, T, cx, cy, r, angle, color):
        p.save()
        p.translate(cx, cy)
        p.setPen(self._pen(T.muted, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), r + 6, r + 6)
        p.rotate(angle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for k in range(7):
            blade = QPainterPath()
            blade.moveTo(0, 0)
            blade.cubicTo(QPointF(r * 0.2, -r * 0.35), QPointF(r * 0.9, -r * 0.45), QPointF(r, -r * 0.05))
            blade.cubicTo(QPointF(r * 0.7, r * 0.05), QPointF(r * 0.3, r * 0.05), QPointF(0, 0))
            p.drawPath(blade)
            p.rotate(360 / 7)
        p.setBrush(T.surface)
        p.drawEllipse(QPointF(0, 0), r * 0.22, r * 0.22)
        p.restore()

    def _draw_fan(self, p, T):
        ang = self.phase * 360 * 1.6 * self.speed
        self._rotor(p, T, -44, 0, 36, ang, T.series[0])
        self._rotor(p, T, 44, 0, 36, ang * 1.08, T.series[1])
        p.setPen(T.muted)
        f = QFont(self.font())
        f.setPointSizeF(7)
        p.setFont(f)
        p.drawText(QRectF(-80, 46, 72, 14), Qt.AlignmentFlag.AlignHCenter, "CPU")
        p.drawText(QRectF(8, 46, 72, 14), Qt.AlignmentFlag.AlignHCenter, "GPU")

    def _chip(self, p, T, w, label, glow):
        hot = mix(T.accent, T.err, glow)
        p.setPen(self._pen(T.muted, 3))
        for k in range(5):
            o = -w / 2 + w * (k + 0.5) / 5
            for a, b in (((o, -w / 2), (o, -w / 2 - 10)), ((o, w / 2), (o, w / 2 + 10)),
                         ((-w / 2, o), (-w / 2 - 10, o)), ((w / 2, o), (w / 2 + 10, o))):
                p.drawLine(QPointF(*a), QPointF(*b))
        p.setPen(self._pen(hot, 4))
        p.setBrush(mix(T.surface, hot, 0.25))
        p.drawRoundedRect(QRectF(-w / 2, -w / 2, w, w), 10, 10)
        p.setPen(T.text)
        f = QFont(self.font())
        f.setPointSizeF(13)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(-w / 2, -w / 2, w, w), Qt.AlignmentFlag.AlignCenter, label)

    def _heat(self, p, T, top):
        for k in range(3):
            x = -24 + 24 * k
            a = (self.phase * 0.9 + k * 0.33) % 1.0
            c = QColor(T.err)
            c.setAlphaF(max(0.0, 0.9 * (1 - a)))
            p.setPen(self._pen(c, 4))
            y0 = top - 6 - 34 * a
            path = QPainterPath(QPointF(x, y0))
            path.cubicTo(QPointF(x - 8, y0 - 8), QPointF(x + 8, y0 - 16), QPointF(x, y0 - 24))
            p.drawPath(path)

    def _draw_cpu(self, p, T):
        self._chip(p, T, 84, "CPU", self._wave(2.0) * self.speed)
        if self.speed > 0.3:
            self._heat(p, T, -54)

    def _draw_gpu(self, p, T):
        p.setPen(self._pen(T.muted, 3))
        p.setBrush(T.surface2)
        p.drawRoundedRect(QRectF(-84, -40, 168, 80), 10, 10)
        p.setBrush(mix(T.surface, T.text, 0.2))
        p.drawRect(QRectF(-70, 40, 60, 10))
        ang = self.phase * 360 * 1.4 * self.speed
        self._rotor(p, T, -38, 0, 24, ang, T.series[0])
        self._rotor(p, T, 38, 0, 24, ang, T.series[0])
        if self.speed < 0.05:
            self._zzz(p, T, 54, -48)

    def _zzz(self, p, T, x, y):
        f = QFont(self.font())
        for k in range(3):
            a = (self.phase * 0.5 + k / 3) % 1.0
            c = QColor(T.accent)
            c.setAlphaF(max(0.0, 1 - a))
            p.setPen(c)
            f.setPointSizeF(10 + 6 * a)
            f.setWeight(QFont.Weight.Bold)
            p.setFont(f)
            p.drawText(QPointF(x + 20 * a, y - 40 * a), "z")

    def _draw_sleep(self, p, T):
        moon = QPainterPath()
        moon.addEllipse(QPointF(-10, 6), 54, 54)
        cut = QPainterPath()
        cut.addEllipse(QPointF(16, -12), 48, 48)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.accent)
        p.drawPath(moon.subtracted(cut))
        self._zzz(p, T, 36, -30)

    def _monitor(self, p, T, screen: QColor):
        p.setPen(self._pen(T.muted, 4))
        p.setBrush(screen)
        p.drawRoundedRect(QRectF(-80, -56, 160, 100), 8, 8)
        p.drawLine(QPointF(0, 44), QPointF(0, 62))
        p.drawLine(QPointF(-30, 64), QPointF(30, 64))

    def _draw_screen_off(self, p, T):
        t = min(1.0, (self.phase % 4.0) / 2.5)
        self._monitor(p, T, mix(mix(T.accent, T.surface, 0.4), QColor("#050505"), t))
        # padlock
        p.setPen(self._pen(T.text if t < 0.5 else T.muted, 4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(-14, -36, 28, 30), 0, 180 * 16)
        p.setBrush(T.text if t < 0.5 else T.muted)
        p.drawRoundedRect(QRectF(-20, -22, 40, 30), 5, 5)

    def _draw_wake(self, p, T):
        self._monitor(p, T, mix(T.accent, T.surface, 0.45))
        dx = 14 * math.sin(self.phase * 8)
        p.setPen(self._pen(T.text, 3))
        p.setBrush(T.surface)
        p.drawRoundedRect(QRectF(-14 + dx, 70, 28, 40), 13, 13)
        p.drawLine(QPointF(dx, 70), QPointF(dx, 84))

    def _draw_measure(self, p, T):
        # bars that settle: "measuring"
        p.setPen(Qt.PenStyle.NoPen)
        for k in range(5):
            h = 30 + 50 * abs(math.sin(self.phase * 1.3 + k * 0.9))
            p.setBrush(T.series[k % 3])
            p.drawRoundedRect(QRectF(-70 + k * 30, 50 - h, 20, h), 5, 5)
        p.setPen(self._pen(T.muted, 3))
        p.drawLine(QPointF(-80, 52), QPointF(80, 52))

    def _badge(self, p, color, draw):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        grow = min(1.0, self.phase * 3)
        p.drawEllipse(QPointF(0, 0), 64 * grow, 64 * grow)
        p.setPen(self._pen(QColor("#ffffff"), 12))
        draw(min(1.0, max(0.0, (self.phase - 0.25) * 2.5)))

    def _draw_done(self, p, T):
        def tick(t):
            path = QPainterPath(QPointF(-28, 2))
            path.lineTo(QPointF(-28 + 20 * min(1, t * 2), 2 + 20 * min(1, t * 2)))
            if t > 0.5:
                u = (t - 0.5) * 2
                path.lineTo(QPointF(-8 + 40 * u, 22 - 44 * u))
            p.drawPath(path)
        self._badge(p, T.ok, tick)

    def _draw_warn(self, p, T):
        def mark(t):
            if t > 0:
                p.drawLine(QPointF(0, -30), QPointF(0, -30 + 40 * t))
            if t >= 1:
                p.drawPoint(QPointF(0, 28))
        self._badge(p, T.warn, mark)

    def _draw_fail(self, p, T):
        def cross(t):
            p.drawLine(QPointF(-24, -24), QPointF(-24 + 48 * min(1, t * 2), -24 + 48 * min(1, t * 2)))
            if t > 0.5:
                u = (t - 0.5) * 2
                p.drawLine(QPointF(24, -24), QPointF(24 - 48 * u, -24 + 48 * u))
        self._badge(p, T.err, cross)
