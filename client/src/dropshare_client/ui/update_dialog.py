"""Offer a downloaded, verified update (FR-UPD-03, FR-UPD-04): install now, or at the next
start. Escape (Not now) decides nothing; the update stays ready in Settings.

The dialog stays open while the choice is carried out (the installer is checked once more, or
the choice saved), saying so, and shows any failure itself; installing now then quits the app.
"""

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.update_controller import UpdateController, UpdateState
from dropshare_client.ui import theme
from dropshare_client.ui.forms import announce, message_label, show_message, status_label

NOW = "&Restart and update"
ON_RESTART = "Update on &next restart"
NOT_NOW = "Not now"
CLOSE = "Close"


class UpdateDialog(QDialog):
    def __init__(self, updates: UpdateController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updates = updates
        self._acted = False
        release = updates.release
        version = release.version if release else ""
        self.setWindowTitle("Update DropShare")
        self.setAccessibleName(self.windowTitle())
        self.setMinimumWidth(theme.SPACE.xl * 20)
        layout = QVBoxLayout(self)

        intro = messages.update_ready(version)
        if updates.urgent:
            intro = f"{messages.UPDATE_REQUIRED} {intro}"
        text = QLabel(f"{intro} DropShare restarts to install it; syncing carries on afterwards.")
        text.setWordWrap(True)
        layout.addWidget(text)
        self.setAccessibleDescription(text.text())  # read out with the dialog's title

        if release and release.notes:
            heading = QLabel("&What's new")
            heading.setProperty("textRole", "subtitle")
            notes = QPlainTextEdit(release.notes)
            notes.setReadOnly(True)
            notes.setAccessibleName("Release notes")
            heading.setBuddy(notes)
            layout.addWidget(heading)
            layout.addWidget(notes)

        self._error = message_label("updateError")
        self._status = status_label("updateStatus")
        layout.addWidget(self._error)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox()
        self._now = buttons.addButton(NOW, QDialogButtonBox.ButtonRole.AcceptRole)
        self._on_restart = buttons.addButton(ON_RESTART, QDialogButtonBox.ButtonRole.ActionRole)
        self._close = buttons.addButton(NOT_NOW, QDialogButtonBox.ButtonRole.RejectRole)
        self._now.setDefault(True)
        buttons.accepted.connect(self._install_now)
        self._on_restart.clicked.connect(self._install_on_restart)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        updates.changed.connect(self._refresh)
        self._refresh()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._now.setFocus()  # not the release notes, where Enter would install

    def _install_now(self) -> None:
        self._acted = True
        show_message(self._error, "")
        self._updates.install_now()

    def _install_on_restart(self) -> None:
        self._acted = True
        show_message(self._error, "")
        self._updates.install_on_restart()

    def _refresh(self) -> None:
        updates = self._updates
        if self._acted and updates.on_restart and not updates.busy:
            self.accept()  # the choice is saved; Settings says so
            return
        ready = updates.state is UpdateState.READY
        self._now.setEnabled(ready and not updates.busy)
        self._on_restart.setEnabled(ready and not updates.busy)
        self._on_restart.setVisible(not updates.on_restart)
        if updates.busy:
            announce(self._status, updates.status)
        elif self._acted:
            announce(self._status, "")
            failure = updates.error or (
                "" if ready or updates.state is UpdateState.INSTALLING else updates.status
            )
            if failure:
                show_message(self._error, failure)
        if not ready and not updates.busy:
            self._close.setText(CLOSE)  # nothing to install from here any more
