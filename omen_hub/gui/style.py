"""Design tokens taken from the desktop palette (panna / panna-dark with the
teal accent), so the hub looks like the rest of the Plasma session in both
light and dark mode."""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPalette
from PyQt6.QtWidgets import QApplication


def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


@dataclass
class Tokens:
    dark: bool
    bg: QColor
    sidebar: QColor
    surface: QColor
    surface2: QColor
    border: QColor
    text: QColor
    muted: QColor
    faint: QColor
    accent: QColor
    on_accent: QColor
    ok: QColor
    warn: QColor
    err: QColor
    off: QColor
    series: tuple            # categorical order, never cycled
    grid: QColor

    def hex(self, name: str) -> str:
        return getattr(self, name).name()


# dataviz reference palette, first three slots (validated all-pairs both modes)
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a")
SERIES_DARK = ("#3987e5", "#d95926", "#199e70")


def tokens(app: QApplication | None = None) -> Tokens:
    pal = (app or QApplication.instance()).palette()
    bg = pal.color(QPalette.ColorRole.Window)
    text = pal.color(QPalette.ColorRole.WindowText)
    dark = bg.lightness() < 128
    accent = pal.color(QPalette.ColorRole.Highlight)
    white, black = QColor("#ffffff"), QColor("#000000")
    if dark:
        surface = mix(bg, white, 0.045)
        surface2 = mix(bg, white, 0.09)
        sidebar = mix(bg, black, 0.18)
    else:
        surface = mix(bg, white, 0.55)
        surface2 = mix(bg, black, 0.04)
        sidebar = mix(bg, black, 0.035)
    return Tokens(
        dark=dark, bg=bg, sidebar=sidebar, surface=surface, surface2=surface2,
        border=mix(bg, text, 0.12), text=text, muted=mix(bg, text, 0.62),
        faint=mix(bg, text, 0.38), accent=accent,
        on_accent=pal.color(QPalette.ColorRole.HighlightedText),
        ok=QColor("#3fb27f" if dark else "#1f8a5a"),
        warn=QColor("#e0a030" if dark else "#b7791f"),
        err=QColor("#e66767" if dark else "#c43c3c"),
        off=mix(bg, text, 0.4),
        series=tuple(QColor(c) for c in (SERIES_DARK if dark else SERIES_LIGHT)),
        grid=mix(bg, text, 0.08),
    )


def number_font(size: float, weight=QFont.Weight.DemiBold) -> QFont:
    """Big readouts: the desktop's dot-matrix face when installed (it is the
    session font here), otherwise the default font."""
    fams = QFontDatabase.families()
    f = QFont("Ndot 57") if "Ndot 57" in fams else QApplication.font()
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def stylesheet(t: Tokens) -> str:
    return f"""
    QMainWindow, #root {{ background: {t.hex('bg')}; }}
    QWidget {{ color: {t.hex('text')}; }}
    #sidebar {{ background: {t.hex('sidebar')}; border-right: 1px solid {t.hex('border')}; }}
    #brand {{ font-size: 15pt; font-weight: 600; padding: 4px 6px 0 6px; }}
    #brandSub {{ color: {t.hex('muted')}; padding: 0 6px; }}
    QPushButton#nav {{
        text-align: left; padding: 9px 12px; border: none; border-radius: 9px;
        background: transparent; color: {t.hex('muted')}; font-size: 10.5pt;
    }}
    QPushButton#nav:hover {{ background: {t.hex('surface2')}; color: {t.hex('text')}; }}
    QPushButton#nav:checked {{ background: {t.hex('surface2')}; color: {t.hex('text')};
        border-left: 3px solid {t.hex('accent')}; }}
    #pageTitle {{ font-size: 18pt; font-weight: 600; }}
    #pageSub {{ color: {t.hex('muted')}; }}
    #card {{ background: {t.hex('surface')}; border: 1px solid {t.hex('border')}; border-radius: 14px; }}
    #cardTitle {{ font-size: 11pt; font-weight: 600; }}
    #cardSub, #muted {{ color: {t.hex('muted')}; }}
    #faint {{ color: {t.hex('faint')}; }}
    #statLabel {{ color: {t.hex('muted')}; font-size: 9pt; }}
    #banner {{ background: {t.hex('surface2')}; border: 1px solid {t.hex('warn')}; border-radius: 10px; }}
    QPushButton {{
        background: {t.hex('surface2')}; border: 1px solid {t.hex('border')};
        border-radius: 8px; padding: 6px 14px;
    }}
    QPushButton:hover {{ border-color: {t.hex('accent')}; }}
    QPushButton:disabled {{ color: {t.hex('faint')}; }}
    QPushButton#primary {{ background: {t.hex('accent')}; color: {t.hex('on_accent')}; border: none; font-weight: 600; }}
    QPushButton#primary:disabled {{ background: {t.hex('surface2')}; color: {t.hex('faint')}; }}
    QPushButton#danger {{ color: {t.hex('err')}; }}
    QPushButton#ghost {{ background: transparent; border: none; color: {t.hex('muted')}; padding: 4px 8px; }}
    QPushButton#ghost:hover {{ color: {t.hex('text')}; }}
    QPushButton#seg {{
        border-radius: 0; border: 1px solid {t.hex('border')}; padding: 7px 14px;
        background: {t.hex('surface')};
    }}
    QPushButton#seg:checked {{ background: {t.hex('accent')}; color: {t.hex('on_accent')}; border-color: {t.hex('accent')}; }}
    QPushButton#segFirst {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
        background: {t.hex('surface2')}; border: 1px solid {t.hex('border')};
        border-radius: 7px; padding: 5px 8px; min-height: 20px;
    }}
    QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {t.hex('accent')}; }}
    QComboBox QAbstractItemView {{ background: {t.hex('surface')}; selection-background-color: {t.hex('accent')}; }}
    QSlider::groove:horizontal {{ height: 6px; border-radius: 3px; background: {t.hex('surface2')}; }}
    QSlider::sub-page:horizontal {{ height: 6px; border-radius: 3px; background: {t.hex('accent')}; }}
    QSlider::handle:horizontal {{
        width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
        background: {t.hex('text')};
    }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ width: 10px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: {t.hex('surface2')}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QToolTip {{ background: {t.hex('surface2')}; color: {t.hex('text')}; border: 1px solid {t.hex('border')}; padding: 4px; }}
    QPlainTextEdit {{ background: {t.hex('sidebar')}; border: 1px solid {t.hex('border')}; border-radius: 8px;
        font-family: "JetBrainsMono Nerd Font", monospace; font-size: 9pt; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; }}
    QListWidget {{ background: {t.hex('surface2')}; border: 1px solid {t.hex('border')};
        border-radius: 10px; padding: 4px; outline: none; }}
    QListWidget::item {{ padding: 7px 8px; border-radius: 7px; }}
    QListWidget::item:hover {{ background: {t.hex('surface')}; }}
    QListWidget::item:selected {{ background: {t.hex('accent')}; color: {t.hex('on_accent')}; }}
    QListWidget:disabled {{ color: {t.hex('faint')}; }}
    """
