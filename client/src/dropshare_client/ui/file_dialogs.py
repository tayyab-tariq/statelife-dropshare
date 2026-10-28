"""Small dialogs for the file browser's actions (FR-FILE-04): a name, checked as typed with
the server's rules (NFR-COMPAT-02), and a confirmation before moving items to the Recycle Bin.
"""

from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.ui.forms import add_field, message_label, show_message
from dropshare_shared.naming import InvalidNameError, validate_name

CANCEL = "Cancel"
_SHOWN_NAMES = 3


class NameDialog(QDialog):
    def __init__(
        self, title: str, action: str, name: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self._field = QLineEdit(name)
        self._error = message_label("name_error")
        form = QFormLayout()
        add_field(form, "&Name", self._field)
        buttons = QDialogButtonBox()
        self._ok = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        self._ok.setDefault(True)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._error)
        layout.addWidget(buttons)
        self._field.textChanged.connect(lambda _: self._check())
        self._check()
        # Select the name without its extension, as Explorer does on rename.
        stem = name.rpartition(".")[0] if "." in name.lstrip(".") else name
        self._field.setSelection(0, len(stem))

    @property
    def name(self) -> str:
        return self._field.text().strip()

    def _check(self) -> bool:
        problem = ""
        if self.name:
            try:
                validate_name(self.name)
            except InvalidNameError as exc:
                problem = str(exc)
        if problem != self._error.text():  # announced once, not on every keystroke
            show_message(self._error, problem)
        self._ok.setEnabled(bool(self.name) and not problem)
        return bool(self.name) and not problem

    def _accept_if_valid(self) -> None:
        if self._check():
            self.accept()


class RecycleDialog(QDialog):
    """Destructive, so the item names are in the text and Cancel is the default."""

    MOVE = "Move to Recycle Bin"

    def __init__(self, names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move to Recycle Bin")
        self.setAccessibleName(self.windowTitle())
        shown = ", ".join(f"“{name}”" for name in names[:_SHOWN_NAMES])
        more = len(names) - _SHOWN_NAMES
        if more > 0:
            shown = f"{shown} and {more} more"
        text = QLabel(
            f"Move {shown} to the Recycle Bin? They're also removed from DropShare on your "
            "other computers, and kept in the DropShare trash where you can restore them."
        )
        text.setWordWrap(True)
        self.setAccessibleDescription(text.text())
        buttons = QDialogButtonBox()
        move = buttons.addButton(self.MOVE, QDialogButtonBox.ButtonRole.AcceptRole)
        keep = buttons.addButton(CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        move.setAutoDefault(False)
        move.setProperty("variant", "danger")
        keep.setDefault(True)
        self._keep: QPushButton = keep
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(buttons)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._keep.setFocus()
