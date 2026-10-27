"""Confirm sign-out and choose what happens to the local files (FR-AUTH-04)."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

_SIGN_OUT = "Sign out"
_SIGN_OUT_AND_MOVE = "Sign out and move files"


class SignOutDialog(QDialog):
    def __init__(self, sync_folder: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign out of DropShare")
        self.setAccessibleName(self.windowTitle())
        layout = QVBoxLayout(self)
        text = QLabel("Syncing stops when you sign out.")
        text.setWordWrap(True)
        layout.addWidget(text)

        self._keep = QRadioButton("&Keep the files on this computer")
        # The folder is named on screen: the user must see what will be moved (ui.md §5).
        self._remove = QRadioButton(f"&Move {sync_folder} to the Recycle Bin")
        self._keep.setChecked(True)
        if sync_folder:
            layout.addWidget(self._keep)
            layout.addWidget(self._remove)
        else:
            self._keep.hide()
            self._remove.hide()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self._confirm = buttons.addButton(_SIGN_OUT, QDialogButtonBox.ButtonRole.AcceptRole)
        self._confirm.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._remove.toggled.connect(
            lambda on: self._confirm.setText(_SIGN_OUT_AND_MOVE if on else _SIGN_OUT)
        )

    @property
    def remove_files(self) -> bool:
        return self._remove.isChecked()
