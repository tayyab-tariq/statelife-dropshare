"""Titled cards of caption/value rows, as on the overview and in Settings (ui.md §2)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.ui import theme


class Value(QLabel):
    """A value next to a visible row label. Its accessible name says both ("Server:
    https://…"), so a screen reader announces the value with what it is."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self._caption = caption
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def set_value(self, value: str) -> None:
        self.setText(value)
        self.setAccessibleName(f"{self._caption}: {value}" if value else self._caption)


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("textRole", "caption")
    return label


def card(heading: str, rows: list[tuple[str, QWidget]]) -> tuple[QFrame, QVBoxLayout]:
    """A titled card of caption/value rows; returns its layout for anything added below."""
    frame = QFrame()
    return frame, fill_card(frame, heading, rows)


def fill_card(frame: QFrame, heading: str, rows: list[tuple[str, QWidget]]) -> QVBoxLayout:
    """Make `frame` a titled card (a widget class can be its own card this way)."""
    space = theme.SPACE
    frame.setProperty("card", True)
    title = QLabel(heading)
    title.setProperty("textRole", "subtitle")
    form = QFormLayout()
    form.setHorizontalSpacing(space.xl)
    form.setVerticalSpacing(space.md)
    for caption, value in rows:
        form.addRow(_caption(caption), value)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(space.xl, space.lg, space.xl, space.xl)
    layout.setSpacing(space.lg)
    layout.addWidget(title)
    layout.addLayout(form)
    return layout


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    """Buttons side by side at the start of a card's last row."""
    row = QHBoxLayout()
    row.setSpacing(theme.SPACE.sm)
    for button in buttons:
        row.addWidget(button)
    row.addStretch()
    return row
