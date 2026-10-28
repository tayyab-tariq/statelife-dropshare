"""The app's look, defined once (visual-design.md): colour tokens, spacing, the stylesheet built
from them, the logo, and the tray icons. Status is never shown by colour alone: the tray tooltip
and window text always say it in words too.

Widgets never set colours or font sizes themselves; they set `textRole` / `variant` properties
that the stylesheet targets. In high-contrast mode none of this applies: the stock style and the
system palette are used so the user's contrast theme wins."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt
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
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


class TrayState(StrEnum):
    """FR-CLIENT-01 tray states, with the words shown for each."""

    UP_TO_DATE = "Up to date"
    SYNCING = "Syncing"
    PAUSED = "Paused"
    OFFLINE = "Offline"
    ERROR = "Error"
    SIGNED_OUT = "Signed out"


# Brand palette. PLACEHOLDER VALUES until the real State Life colours are supplied
# (visual-design.md §2.1); everything else is derived from these.
_BRAND = {
    "primary": "#0B6E4F",
    "primary_hover": "#095C42",
    "primary_pressed": "#074A35",
    "secondary": "#1F4E79",
    "on_primary": "#FFFFFF",
}
_BRAND_DIR = Path(__file__).resolve().parent.parent / "resources" / "brand"

_FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"]
_ICON_SIZES = (16, 20, 24, 32, 48)
_AA_CONTRAST = 4.5


@dataclass(frozen=True)
class Spacing:
    """The spacing scale in logical px; layouts use only these."""

    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32


SPACE = Spacing()
DIALOG_WIDTH = 400  # sign-in and the dialogs that share its frame
BRAND_STRIP_HEIGHT = 4
LOGO_HEIGHT = 64
HEADER_MARK_SIZE = 24
MAIN_WINDOW_MIN_SIZE = QSize(900, 600)  # visual-design.md §6, with the sidebar
SIDEBAR_WIDTH = 220
PROGRESS_COLUMN_WIDTH = 120  # the Transfers view's progress bars
STATE_MAX_WIDTH = 360  # width of the messages in the loading, error and empty states


@dataclass(frozen=True)
class Tokens:
    dark: bool
    primary: QColor
    primary_hover: QColor
    primary_pressed: QColor
    on_primary: QColor
    bg_window: QColor
    bg_surface: QColor
    bg_hover: QColor  # an overlay, blended onto whatever it covers
    bg_selected: QColor
    border: QColor
    text_primary: QColor
    text_secondary: QColor
    text_disabled: QColor
    success: QColor
    warning: QColor
    danger: QColor
    info: QColor


def _mix(colour: QColor, other: QColor, amount: float) -> QColor:
    """`colour` moved `amount` (0 to 1) of the way towards `other`, opaque."""
    return QColor(
        round(colour.red() + (other.red() - colour.red()) * amount),
        round(colour.green() + (other.green() - colour.green()) * amount),
        round(colour.blue() + (other.blue() - colour.blue()) * amount),
    )


def _over(base: QColor, overlay: QColor) -> QColor:
    """An alpha overlay flattened onto an opaque base."""
    return _mix(base, QColor(overlay.rgb()), overlay.alphaF())


def _alpha(colour: QColor, alpha: float) -> QColor:
    result = QColor(colour)
    result.setAlphaF(alpha)
    return result


def _luminance(colour: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in (colour.redF(), colour.greenF(), colour.blueF()))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG 2 contrast ratio, 1 to 21."""
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def tokens_for(dark: bool) -> Tokens:
    white = QColor("#FFFFFF")
    if dark:
        # A lighter tint of the same hue reads on dark surfaces (visual-design.md §2.1).
        base = QColor(_BRAND["primary"])
        primary = _mix(base, white, 0.3)
        hover, pressed = _mix(base, white, 0.4), _mix(base, white, 0.25)
        text = QColor("#F2F3F5")
        surface = QColor("#262729")
        # White fails AA on the tint, so text on primary turns dark instead.
        on_primary = max(
            (QColor(_BRAND["on_primary"]), QColor("#1B1C1E")),
            key=lambda c: contrast_ratio(c, pressed),
        )
        return Tokens(
            dark=True,
            primary=primary,
            primary_hover=hover,
            primary_pressed=pressed,
            on_primary=on_primary,
            bg_window=QColor("#1C1D1F"),
            bg_surface=surface,
            bg_hover=QColor(255, 255, 255, 15),
            bg_selected=_alpha(primary, 0.2),
            border=QColor("#3A3B3E"),
            text_primary=text,
            text_secondary=QColor("#A8ABB0"),
            text_disabled=QColor("#6B6E72"),
            success=QColor("#6CCB5F"),
            warning=QColor("#FCE100"),
            danger=QColor("#FF99A4"),
            info=_mix(QColor(_BRAND["secondary"]), white, 0.45),
        )
    primary = QColor(_BRAND["primary"])
    return Tokens(
        dark=False,
        primary=primary,
        primary_hover=QColor(_BRAND["primary_hover"]),
        primary_pressed=QColor(_BRAND["primary_pressed"]),
        on_primary=QColor(_BRAND["on_primary"]),
        bg_window=QColor("#F7F8FA"),
        bg_surface=white,
        bg_hover=QColor(0, 0, 0, 10),
        bg_selected=_alpha(primary, 0.12),
        border=QColor("#E1E4E8"),
        text_primary=QColor("#1B1C1E"),
        text_secondary=QColor("#5F6368"),
        text_disabled=QColor("#A0A4A8"),
        success=QColor("#107C10"),
        warning=QColor("#9D5D00"),
        danger=QColor("#C42B1C"),
        info=QColor(_BRAND["secondary"]),
    )


def _dark_scheme() -> bool:
    # The system setting, not the palette: the palette is ours once the theme is applied.
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _high_contrast() -> bool:
    # Keep each object referenced: a chained call lets PySide free the temporary wrapper.
    hints = QGuiApplication.styleHints()
    accessibility = hints.accessibility()
    return accessibility.contrastPreference() == Qt.ContrastPreference.HighContrast


def current_tokens() -> Tokens:
    return tokens_for(_dark_scheme())


def _css(colour: QColor) -> str:
    if colour.alpha() == 255:
        return colour.name()
    return f"rgba({colour.red()}, {colour.green()}, {colour.blue()}, {colour.alpha()})"


def build_palette(t: Tokens) -> QPalette:
    """For what Fusion paints itself (check boxes, radio buttons, focus, text selection)."""
    roles = QPalette.ColorRole
    palette = QPalette()
    for role, colour in (
        (roles.Window, t.bg_window),
        (roles.WindowText, t.text_primary),
        (roles.Base, t.bg_surface),
        (roles.AlternateBase, t.bg_window),
        (roles.Text, t.text_primary),
        (roles.Button, t.bg_surface),
        (roles.ButtonText, t.text_primary),
        (roles.Highlight, t.primary),
        (roles.HighlightedText, t.on_primary),
        (roles.PlaceholderText, t.text_secondary),
        (roles.ToolTipBase, t.bg_surface),
        (roles.ToolTipText, t.text_primary),
        (roles.Link, t.primary),
        (roles.Light, t.bg_surface),
        (roles.Midlight, t.border),
        (roles.Mid, t.border),
        (roles.Dark, t.text_disabled),
        (roles.Shadow, t.text_disabled),
    ):
        palette.setColor(role, colour)
    for role in (roles.WindowText, roles.Text, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, t.text_disabled)
    return palette


def build_stylesheet(t: Tokens, body_pt: float) -> str:
    """The one app stylesheet. Font sizes scale from the system body size, so the user's
    Windows text-size setting carries through."""
    s = SPACE
    hover = _over(t.bg_surface, t.bg_hover)
    pressed = _over(hover, t.bg_hover)
    return f"""
QDialog {{ background: {_css(t.bg_surface)}; }}
QMainWindow {{ background: {_css(t.bg_window)}; }}

QLabel[textRole="title"] {{ font-size: {body_pt * 1.6:.1f}pt; font-weight: 600; }}
QLabel[textRole="subtitle"] {{ font-size: {body_pt * 1.25:.1f}pt; font-weight: 600; }}
QLabel[textRole="caption"] {{
    font-size: {body_pt * 0.9:.1f}pt; color: {_css(t.text_secondary)};
}}
QLabel[textRole="error"] {{ color: {_css(t.danger)}; }}
QFrame#brandStrip {{ background: {_css(t.primary)}; border: none; }}
QFrame#appHeader {{
    background: {_css(t.bg_surface)}; border: none; border-bottom: 1px solid {_css(t.border)};
}}
QFrame[card="true"] {{
    background: {_css(t.bg_surface)}; border: 1px solid {_css(t.border)}; border-radius: 8px;
}}

QPushButton {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_primary)};
    border: 1px solid {_css(t.border)}; border-radius: 4px;
    padding: 6px {s.lg}px; min-height: 18px;
}}
QPushButton:hover {{ background: {_css(hover)}; }}
QPushButton:pressed {{ background: {_css(pressed)}; }}
QPushButton:focus {{ border: 2px solid {_css(t.primary)}; padding: 5px {s.lg - 1}px; }}
QPushButton:disabled {{ color: {_css(t.text_disabled)}; background: {_css(t.bg_window)}; }}
QPushButton[variant="primary"] {{
    background: {_css(t.primary)}; color: {_css(t.on_primary)};
    border-color: {_css(t.primary)}; font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {_css(t.primary_hover)}; }}
QPushButton[variant="primary"]:pressed {{ background: {_css(t.primary_pressed)}; }}
QPushButton[variant="primary"]:focus {{ border-color: {_css(t.text_primary)}; }}
QPushButton[variant="primary"]:disabled {{
    background: {_css(t.border)}; border-color: {_css(t.border)};
    color: {_css(t.text_disabled)};
}}
QPushButton[variant="subtle"] {{ background: transparent; border-color: transparent; }}
QPushButton[variant="subtle"]:hover {{ background: {_css(hover)}; }}
QPushButton[variant="subtle"]:pressed {{ background: {_css(pressed)}; }}
QPushButton[variant="subtle"]:focus {{ border-color: {_css(t.primary)}; }}
QPushButton[variant="subtle"]:disabled {{ background: transparent; }}

QLineEdit {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_primary)};
    border: 1px solid {_css(t.border)}; border-radius: 4px;
    padding: 0 {s.sm}px; min-height: 30px;
    selection-background-color: {_css(t.primary)}; selection-color: {_css(t.on_primary)};
}}
QLineEdit:hover {{ border-color: {_css(t.text_disabled)}; }}
QLineEdit:focus {{ border: 2px solid {_css(t.primary)}; padding: 0 {s.sm - 1}px; }}
QLineEdit:disabled {{ color: {_css(t.text_disabled)}; background: {_css(t.bg_window)}; }}

QCheckBox, QRadioButton {{ spacing: {s.sm}px; }}

QProgressBar {{
    background: {_css(t.border)}; border: none; border-radius: 3px;
    min-height: 6px; max-height: 6px;
}}
QProgressBar::chunk {{ background: {_css(t.primary)}; border-radius: 3px; }}

#navList {{
    background: {_css(t.bg_window)}; border: none; outline: none;
    border-right: 1px solid {_css(t.border)}; padding: {s.sm}px 0;
}}
#navList::item {{
    min-height: 40px; padding: 0 {s.lg}px; color: {_css(t.text_primary)};
    border-left: 3px solid transparent;
}}
#navList::item:hover {{ background: {_css(hover)}; }}
#navList::item:selected {{
    background: {_css(t.bg_selected)}; color: {_css(t.text_primary)};
    border-left: 3px solid {_css(t.primary)};
}}
#navList:focus {{ border-right: 1px solid {_css(t.primary)}; }}

QTableView#fileTable, QTableView#transferTable {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_primary)};
    border: none; gridline-color: transparent;
    selection-background-color: {_css(t.bg_selected)}; selection-color: {_css(t.text_primary)};
}}
QTableView#fileTable::item, QTableView#transferTable::item {{ padding: 0 {s.sm}px; }}
QTableView#fileTable::item:hover, QTableView#transferTable::item:hover {{
    background: {_css(hover)};
}}
QHeaderView::section {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_secondary)};
    border: none; border-bottom: 1px solid {_css(t.border)};
    padding: {s.xs}px {s.sm}px;
}}

QScrollBar:vertical {{ background: transparent; width: {s.sm}px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {s.sm}px; margin: 0; }}
QScrollBar::handle {{ background: {_css(t.border)}; border-radius: {s.xs}px; }}
QScrollBar::handle:hover {{ background: {_css(t.text_disabled)}; }}
QScrollBar::handle:vertical {{ min-height: {s.xxl}px; }}
QScrollBar::handle:horizontal {{ min-width: {s.xxl}px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QMenu {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_primary)};
    border: 1px solid {_css(t.border)}; padding: {s.xs}px 0;
}}
QMenu::item {{ padding: 6px {s.md}px; margin: 0 {s.xs}px; border-radius: 4px; }}
QMenu::item:selected {{ background: {_css(t.bg_selected)}; color: {_css(t.text_primary)}; }}
QMenu::item:disabled {{ color: {_css(t.text_disabled)}; background: transparent; }}
QMenu::separator {{ height: 1px; background: {_css(t.border)}; margin: {s.xs}px 0; }}

QToolTip {{
    background: {_css(t.bg_surface)}; color: {_css(t.text_primary)};
    border: 1px solid {_css(t.border)}; padding: {s.xs}px {s.sm}px;
}}
"""


class Theme(QObject):
    """Applies the tokens to the whole app and follows light/dark and high-contrast changes
    while running. Create once, before any window."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._native_style = app.style().name()
        self._system_font = QFont(app.font())
        size = self._system_font.pointSizeF()
        self._body_pt = size if size > 0 else 9.0
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(lambda _: self.apply())
        accessibility = hints.accessibility()
        accessibility.contrastPreferenceChanged.connect(lambda _: self.apply())
        self.apply()

    def apply(self) -> None:
        # The style goes first: switching it resets the palette.
        if _high_contrast():
            self._app.setStyleSheet("")
            self._app.setStyle(self._native_style)
            self._app.setPalette(QPalette())  # nothing set: the system palette throughout
            self._app.setFont(self._system_font)
            return
        tokens = current_tokens()
        self._app.setStyle("Fusion")
        font = QFont(self._system_font)
        font.setFamilies([*_FONT_FAMILIES, self._system_font.family()])
        self._app.setFont(font)
        self._app.setPalette(build_palette(tokens))
        self._app.setStyleSheet(build_stylesheet(tokens, self._body_pt))


def _render_svg(name: str, width: int, height: int, ratio: float) -> QPixmap:
    pixmap = QPixmap(round(width * ratio), round(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(str(_BRAND_DIR / name)).render(painter, QRectF(0, 0, width, height))
    painter.end()
    return pixmap


def logo(height: int, ratio: float) -> QPixmap:
    """The full logo at `height` logical px, its own aspect ratio, crisp at `ratio` (DPR).
    Replacing the SVG file needs no code change (visual-design.md §7)."""
    size = QSvgRenderer(str(_BRAND_DIR / "logo.svg")).defaultSize()
    width = round(height * size.width() / size.height()) if size.height() > 0 else height
    return _render_svg("logo.svg", width, height, ratio)


def logo_mark(size: int, ratio: float) -> QPixmap:
    """The square mark, for small sizes and the window icon."""
    return _render_svg("logo-mark.svg", size, size, ratio)


def badge_colour(state: TrayState) -> QColor:
    if _high_contrast():
        return QGuiApplication.palette().color(QPalette.ColorRole.WindowText)
    t = current_tokens()
    return {
        TrayState.UP_TO_DATE: t.success,
        TrayState.SYNCING: t.info,
        TrayState.PAUSED: t.warning,
        TrayState.OFFLINE: t.text_secondary,
        TrayState.ERROR: t.danger,
        TrayState.SIGNED_OUT: t.text_secondary,
    }[state]


def status_icon(state: TrayState) -> QIcon:
    """The app mark with a state badge, drawn at every tray size for crisp HiDPI rendering.
    Each state has its own glyph, so it's never told apart by colour alone (NFR-UX-02)."""
    if _high_contrast():
        palette = QGuiApplication.palette()
        fill = palette.color(QPalette.ColorRole.Highlight)
        on_fill = palette.color(QPalette.ColorRole.HighlightedText)
        ring = palette.color(QPalette.ColorRole.Window)
    else:
        t = current_tokens()
        fill, on_fill, ring = t.primary, t.on_primary, t.bg_surface
    icon = QIcon()
    for size in _ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(size * 0.62))
        painter.setFont(font)
        painter.setPen(on_fill)
        painter.drawText(QRectF(0, 0, size * 0.8, size * 0.8), Qt.AlignmentFlag.AlignCenter, "D")
        _draw_badge(painter, state, size, ring)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def _draw_badge(painter: QPainter, state: TrayState, size: int, ring: QColor) -> None:
    d = size * 0.5
    box = QRectF(size - d, size - d, d - 0.5, d - 0.5)
    colour = badge_colour(state)
    painter.setPen(QPen(ring, max(1.0, size / 24)))
    painter.setBrush(ring if state is TrayState.SIGNED_OUT else colour)
    painter.drawEllipse(box)
    glyph = QPen(colour if state is TrayState.SIGNED_OUT else ring, max(1.2, d * 0.14))
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
