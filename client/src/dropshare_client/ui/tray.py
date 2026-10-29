"""System tray icon and menu (FR-CLIENT-01)."""

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from dropshare_client.ui.theme import TrayState, status_icon

_TOOLTIP_MAX = 127  # Windows truncates longer tray tooltips


class Tray(QObject):
    open_folder_requested = Signal()
    open_app_requested = Signal()
    pause_toggled = Signal(bool)  # True: pause
    quit_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._icon = QSystemTrayIcon()
        self._menu = QMenu()
        self._menu.setToolTipsVisible(True)
        # First line: the state in words, with the transfer summary while syncing.
        self.status_line = self._menu.addAction("")
        self.status_line.setEnabled(False)
        self._menu.addSeparator()
        self.open_folder = self._action("Open sync folder", self.open_folder_requested.emit)
        self.open_app = self._action("Open DropShare", self.open_app_requested.emit)
        # Activity and Settings arrive with roadmap F5; listed now so the menu doesn't change.
        self.recent_activity = self._action("Recent activity", None)
        self.pause = self._action("Pause syncing", lambda: self.pause_toggled.emit(True))
        self.resume = self._action("Resume syncing", lambda: self.pause_toggled.emit(False))
        self.settings = self._action("Settings", None)
        self._menu.addSeparator()
        self.quit = self._action("Quit", self.quit_requested.emit)
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._activated)
        self._icon.messageClicked.connect(self.open_app_requested.emit)
        self.state = TrayState.SIGNED_OUT
        self._detail = ""
        self._summary = ""
        self._paused = False
        # Redraw when the theme or contrast setting changes while running.
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(lambda _: self._refresh())
        accessibility = hints.accessibility()
        accessibility.contrastPreferenceChanged.connect(lambda _: self._refresh())
        self._refresh()

    def _action(self, text: str, slot: Callable[[], object] | None) -> QAction:
        action = self._menu.addAction(text)
        if slot is not None:
            action.triggered.connect(slot)
        else:
            action.setEnabled(False)
            action.setToolTip("Coming in a later version")
        return action

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def set_state(self, state: TrayState, detail: str = "", summary: str = "") -> None:
        """Icon, tooltip and the menu's status line; they always say the state in words.
        `summary` is the transfers' ("12 items · 45%", FR-CLIENT-08), shown instead of
        `detail` when given."""
        self.state, self._detail, self._summary = state, detail, summary
        self._refresh()

    def set_paused(self, paused: bool) -> None:
        """Which of Pause/Resume the menu offers; the shown state is set by the shell."""
        self._paused = paused
        self._refresh()

    def tooltip(self) -> str:
        return self._icon.toolTip()

    def notify(self, title: str, message: str) -> None:
        """A balloon/toast; the tray icon's only way to tell the user something unasked."""
        self._icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information)

    def _refresh(self) -> None:
        self._icon.setIcon(status_icon(self.state))
        status = self.state.value
        if self._summary:
            status = f"{status} {self._summary}"
        elif self._detail:
            status = f"{status} ({self._detail})"
        self.status_line.setText(status)
        self._icon.setToolTip(f"StateLife DropShare: {status}"[:_TOOLTIP_MAX])
        signed_in = self.state is not TrayState.SIGNED_OUT
        self.open_folder.setEnabled(signed_in)
        self.pause.setVisible(not self._paused)
        self.pause.setEnabled(signed_in)
        self.resume.setVisible(self._paused)
        self.resume.setEnabled(signed_in)

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_app_requested.emit()
