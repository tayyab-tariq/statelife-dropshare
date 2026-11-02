"""Move the sync folder somewhere else (FR-CLIENT-04): choose the new place, see the copy's
progress, cancel it. The shell starts the move (it knows who is signed in)."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.folder_move_controller import FolderMoveController
from dropshare_client.ui.forms import announce, message_label, show_message, status_label

_STEPS = 1000  # the progress bar's range: bytes don't fit an int past 2 GB


class MoveFolderDialog(QDialog):
    move_requested = Signal(str)

    def __init__(self, controller: FolderMoveController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Move your DropShare folder")
        self.setAccessibleName(self.windowTitle())
        intro = QLabel(messages.MOVE_FOLDER_INTRO)
        intro.setWordWrap(True)
        current = QLabel(f"Now: {controller.current_folder}")
        current.setWordWrap(True)

        label = QLabel("&New folder")
        self._path = QLineEdit()
        self._path.setAccessibleName("New folder")
        label.setBuddy(self._path)
        self._browse = QPushButton("&Browse…")
        row = QHBoxLayout()
        row.addWidget(label)
        row.addWidget(self._path, stretch=1)
        row.addWidget(self._browse)
        self._message = message_label("moveMessage")
        self._status = status_label("moveStatus")  # checking, cancelling
        self._pending: str | None = None  # the path being checked
        self._move_when_checked = False  # Enter was pressed before the check came back
        self._progress = QProgressBar()
        self._progress.setAccessibleName("Moving")
        self._progress.setRange(0, 0)  # until a copy reports its size
        self._progress.hide()

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._move = self._buttons.addButton("&Move", QDialogButtonBox.ButtonRole.AcceptRole)
        self._move.setDefault(True)
        self._move.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(current)
        layout.addLayout(row)
        layout.addWidget(self._message)
        layout.addWidget(self._status)
        layout.addWidget(self._progress)
        layout.addWidget(self._buttons)

        self._browse.clicked.connect(self._choose)
        self._path.textEdited.connect(self._edited)
        self._path.returnPressed.connect(self._enter)
        # Bound methods, not lambdas: Qt disconnects them when the dialog goes away.
        self._path.editingFinished.connect(self._edit_finished)
        controller.checked.connect(self._checked)
        controller.progressed.connect(self._progressed)
        controller.failed.connect(self._failed)
        controller.moved.connect(self._moved)

    def accept(self) -> None:
        """Starts the move; the dialog closes once it is done (or says why not)."""
        if not self._move.isEnabled() or self._controller.busy:
            return
        show_message(self._message, "")
        self._set_moving(True)
        self.move_requested.emit(self._path.text())

    def _moved(self, _: str) -> None:
        super().accept()

    def reject(self) -> None:
        """Cancel: before the move, closes; during a copy, stops it (everything goes back)."""
        if self._controller.busy:
            self._cancel()
        else:
            super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._controller.busy:
            event.ignore()
            self._cancel()
        else:
            super().closeEvent(event)

    def _cancel(self) -> None:
        """Shows that the click registered: a copy stops within a moment, then puts back."""
        self._controller.cancel()
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setEnabled(False)
        announce(self._status, messages.CANCELLING_MOVE)

    def _edited(self, _: str) -> None:
        self._move.setEnabled(False)
        self._move_when_checked = False

    def _enter(self) -> None:
        """Enter in the path field moves, once the path has been checked."""
        if not self._move.isEnabled():
            self._move_when_checked = True
            self._check(self._path.text())

    def _edit_finished(self) -> None:
        self._check(self._path.text())

    def _check(self, path: str) -> None:
        if path == self._pending or self._controller.busy:
            return
        self._pending = path
        self._move.setEnabled(False)
        announce(self._status, messages.CHECKING_FOLDER)
        self._controller.check(path)

    def _choose(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose the new place")
        if chosen:
            self._path.setText(chosen)
            self._check(chosen)

    def _checked(self, path: str, problem: str) -> None:
        if path == self._pending:
            self._pending = None
        if path != self._path.text() or self._controller.busy:
            return
        announce(self._status, "")
        show_message(self._message, problem)
        self._move.setEnabled(not problem)
        go, self._move_when_checked = self._move_when_checked, False
        if go and not problem:
            self.accept()

    def _progressed(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, _STEPS)
            self._progress.setValue(min(_STEPS, done * _STEPS // total))

    def _failed(self, message: str) -> None:
        self._set_moving(False)
        show_message(self._message, message)

    def _set_moving(self, moving: bool) -> None:
        self._progress.setVisible(moving)
        self._progress.setRange(0, 0)
        for widget in (self._path, self._browse, self._move):
            widget.setEnabled(not moving)
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setEnabled(True)
        announce(self._status, messages.MOVING_FOLDER if moving else "")
        if moving:
            show_message(self._message, "")
