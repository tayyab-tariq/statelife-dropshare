"""Ask before a mass delete is applied (FR-SYNC-08). Pausing is the safe default."""

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

DELETE = "Delete files"  # no mnemonic: deleting takes Tab and Enter, or a click
PAUSE = "&Pause syncing"


class MassDeleteDialog(QDialog):
    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm deleting files")
        self.setAccessibleName(self.windowTitle())
        layout = QVBoxLayout(self)
        text = QLabel(message)
        text.setWordWrap(True)
        layout.addWidget(text)
        self.setAccessibleDescription(message)  # read out with the dialog's title

        buttons = QDialogButtonBox()
        self._pause = buttons.addButton(PAUSE, QDialogButtonBox.ButtonRole.RejectRole)
        delete = buttons.addButton(DELETE, QDialogButtonBox.ButtonRole.AcceptRole)
        # Enter never deletes by accident. Buttons in a dialog become the default when they
        # have focus, and Windows lays out "Delete files" first, so pin both explicitly.
        delete.setAutoDefault(False)
        self._pause.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._pause.setFocus()
