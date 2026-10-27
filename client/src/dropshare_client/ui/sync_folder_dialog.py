"""Choose the sync folder on first run (FR-SYNC-01)."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.ui.forms import message_label, show_message


class SyncFolderDialog(QDialog):
    def __init__(self, controller: SyncFolderController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._saving = False
        self.setWindowTitle("Choose your DropShare folder")
        self.setAccessibleName(self.windowTitle())
        intro = QLabel(
            "DropShare keeps a copy of your files in this folder and syncs any changes you make "
            "there. Choose a new or empty folder on a drive inside this computer."
        )
        intro.setWordWrap(True)

        label = QLabel("&Folder")
        self._path = QLineEdit(controller.default_folder)
        self._path.setAccessibleName("Folder")
        label.setBuddy(self._path)
        self._browse_button = QPushButton("&Choose another folder…")
        browse = self._browse_button
        row = QHBoxLayout()
        row.addWidget(label)
        row.addWidget(self._path, stretch=1)
        row.addWidget(browse)
        self._message = message_label("folder_message")

        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._use = self._buttons.addButton(
            "Use this folder", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._use.setDefault(True)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self._message)
        layout.addWidget(self._buttons)

        browse.clicked.connect(self._browse)
        self._path.textEdited.connect(lambda _: show_message(self._message, ""))
        self._path.editingFinished.connect(lambda: controller.check(self._path.text()))
        controller.checked.connect(self._checked)
        controller.save_failed.connect(self._failed)
        controller.check(self._path.text())

    def accept(self) -> None:
        """Validate and create the folder first; the shell closes the dialog once it's saved."""
        self._saving = True
        for widget in (self._use, self._path, self._browse_button):
            widget.setEnabled(False)
        self._controller.save(self._path.text())

    def done_choosing(self) -> None:
        super().accept()

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose your DropShare folder")
        if chosen:
            self._path.setText(chosen)
            self._controller.check(chosen)

    def _checked(self, path: str, problem: str) -> None:
        if path == self._path.text() and not self._saving:
            show_message(self._message, problem)
            self._use.setEnabled(not problem)

    def _failed(self, message: str) -> None:
        self._saving = False
        show_message(self._message, message)
        for widget in (self._use, self._path, self._browse_button):
            widget.setEnabled(True)
