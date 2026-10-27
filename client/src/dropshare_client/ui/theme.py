"""Colours beyond the system palette, defined once (ui.md §1). Status is never shown by colour
alone: the tray tooltip and window text always say it in words too."""

from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)


class TrayState(StrEnum):
    """FR-CLIENT-01 tray states, with the words shown for each."""

    UP_TO_DATE = "Up to date"
    SYNCING = "Syncing"
    PAUSED = "Paused"
    OFFLINE = "Offline"
    ERROR = "Error"
    SIGNED_OUT = "Signed out"


# (light theme, dark theme) badge colours.
_BADGES: dict[TrayState, tuple[str, str]] = {
    TrayState.UP_TO_DATE: ("#1e7d32", "#66bb6a"),
    TrayState.SYNCING: ("#1565c0", "#64b5f6"),
    TrayState.PAUSED: ("#616161", "#bdbdbd"),
    TrayState.OFFLINE: ("#b26a00", "#ffb74d"),
    TrayState.ERROR: ("#c62828", "#ef5350"),
    TrayState.SIGNED_OUT: ("#616161", "#bdbdbd"),
}
_ICON_SIZES = (16, 20, 24, 32, 48)


def _is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def _high_contrast() -> bool:
    # Keep each object referenced: a chained call lets PySide free the temporary wrapper.
    hints = QGuiApplication.styleHints()
    accessibility = hints.accessibility()
    return accessibility.contrastPreference() == Qt.ContrastPreference.HighContrast


def badge_colour(state: TrayState) -> QColor:
    palette = QGuiApplication.palette()
    if _high_contrast():
        return palette.color(QPalette.ColorRole.WindowText)
    light, dark = _BADGES[state]
    return QColor(dark if _is_dark(palette) else light)


def status_icon(state: TrayState) -> QIcon:
    """The app mark with a state badge, drawn at every tray size for crisp HiDPI rendering.
    Each state has its own glyph, so it's never told apart by colour alone (NFR-UX-02)."""
    icon = QIcon()
    palette = QGuiApplication.palette()
    for size in _ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.2, size * 0.2)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.62))
        painter.setFont(font)
        painter.setPen(palette.color(QPalette.ColorRole.HighlightedText))
        painter.drawText(QRectF(0, 0, size * 0.8, size * 0.8), Qt.AlignmentFlag.AlignCenter, "D")
        _draw_badge(painter, state, size, palette)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _draw_badge(painter: QPainter, state: TrayState, size: int, palette: QPalette) -> None:
    d = size * 0.5
    box = QRectF(size - d, size - d, d - 0.5, d - 0.5)
    background = palette.color(QPalette.ColorRole.Window)
    colour = badge_colour(state)
    painter.setPen(QPen(background, max(1.0, size / 24)))
    painter.setBrush(background if state is TrayState.SIGNED_OUT else colour)
    painter.drawEllipse(box)
    glyph = QPen(colour if state is TrayState.SIGNED_OUT else background, max(1.2, d * 0.14))
    glyph.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(glyph)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    x, y, w = box.x(), box.y(), box.width()

    def at(fx: float, fy: float) -> QPointF:
        return QPointF(x + fx * w, y + fy * w)

    match state:
        case TrayState.UP_TO_DATE:  # check mark
            painter.drawPolyline([at(0.27, 0.52), at(0.44, 0.69), at(0.74, 0.33)])
        case TrayState.SYNCING:  # circular arrow
            painter.drawArc(
                box.adjusted(w * 0.25, w * 0.25, -w * 0.25, -w * 0.25), 90 * 16, 270 * 16
            )
            painter.drawPolyline([at(0.5, 0.17), at(0.5, 0.33), at(0.66, 0.25)])
        case TrayState.PAUSED:  # two bars
            painter.drawLine(at(0.4, 0.3), at(0.4, 0.7))
            painter.drawLine(at(0.6, 0.3), at(0.6, 0.7))
        case TrayState.OFFLINE:  # slash
            painter.drawLine(at(0.3, 0.7), at(0.7, 0.3))
        case TrayState.ERROR:  # exclamation mark
            painter.drawLine(at(0.5, 0.27), at(0.5, 0.55))
            painter.drawPoint(at(0.5, 0.72))
        case TrayState.SIGNED_OUT:  # hollow badge with a dash
            painter.drawLine(at(0.32, 0.5), at(0.68, 0.5))
