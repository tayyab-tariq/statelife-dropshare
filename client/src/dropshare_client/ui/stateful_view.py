"""Loading, error, empty and content states for any data-driven view (NFR-UX-01, ui.md §3)."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import (
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

_LOADING_DELAY_MS = 300  # fast loads never flash a spinner


def _page(*widgets: QWidget) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addStretch()
    for widget in widgets:
        layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignHCenter)
    layout.addStretch()
    return page


def _message_label(object_name: str) -> QLabel:
    # No accessible name: screen readers must read the message itself (see forms.py).
    label = QLabel()
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class StatefulView(QStackedWidget):
    retry_requested = Signal()
    empty_action_requested = Signal()

    def __init__(self, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._blank = QWidget()
        busy = QProgressBar()
        busy.setRange(0, 0)  # indeterminate
        busy.setAccessibleName("Loading")
        busy.setTextVisible(False)
        self._loading_label = _message_label("loading_message")
        self._loading = _page(busy, self._loading_label)

        self._error_label = _message_label("error_message")
        self._retry = QPushButton("&Try again")
        self._retry.clicked.connect(self.retry_requested)
        self._error = _page(self._error_label, self._retry)

        self._empty_label = _message_label("empty_message")
        self._empty_button = QPushButton()
        self._empty_button.clicked.connect(self.empty_action_requested)
        self._empty = _page(self._empty_label, self._empty_button)

        self._content = content
        for page in (self._blank, self._loading, self._error, self._empty, content):
            self.addWidget(page)
        self._loading_timer = QTimer(self)
        self._loading_timer.setSingleShot(True)
        self._loading_timer.setInterval(_LOADING_DELAY_MS)
        self._loading_timer.timeout.connect(lambda: self.setCurrentWidget(self._loading))

    def show_loading(self, message: str = "Loading…") -> None:
        """Keeps showing loaded content while refreshing; otherwise shows a spinner after a
        short delay."""
        self._loading_label.setText(message)
        if self.currentWidget() is not self._content:
            self.setCurrentWidget(self._blank)
            self._loading_timer.start()

    def show_error(self, message: str) -> None:
        self._loading_timer.stop()
        self._error_label.setText(message)
        self.setCurrentWidget(self._error)
        self._retry.setFocus()
        QAccessible.updateAccessibility(
            QAccessibleEvent(self._error_label, QAccessible.Event.Alert)
        )

    def show_empty(self, message: str, action_text: str) -> None:
        self._loading_timer.stop()
        self._empty_label.setText(message)
        self._empty_button.setText(action_text)
        self.setCurrentWidget(self._empty)

    def show_content(self) -> None:
        self._loading_timer.stop()
        self.setCurrentWidget(self._content)

    def current_state(self) -> str:
        pages = {
            id(self._blank): "loading",
            id(self._loading): "loading",
            id(self._error): "error",
            id(self._empty): "empty",
            id(self._content): "content",
        }
        return pages[id(self.currentWidget())]
