"""Dialogs of an upload from the app (FR-FILE-02): what to do with a name already used, and
the upload's progress with Cancel (OQ-28)."""

from PySide6.QtCore import Signal
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.upload_controller import Resolution
from dropshare_client.ui import theme

CANCEL_UPLOAD = "Cancel upload? Everything copied so far will be removed."


def _framed(dialog: QDialog) -> QVBoxLayout:
    """visual-design.md §3: dialog margins and width from the tokens."""
    dialog.setMinimumWidth(theme.DIALOG_WIDTH)
    layout = QVBoxLayout(dialog)
    space = theme.SPACE
    layout.setContentsMargins(space.xl, space.xl, space.xl, space.xl)
    layout.setSpacing(space.md)
    return layout


class ClashDialog(QDialog):
    """Keep both is the default: it never replaces anything."""

    KEEP_BOTH = "&Keep both"
    REPLACE = "&Replace"
    SKIP = "&Skip"
    CANCEL = "Cancel upload"

    def __init__(self, name: str, others: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Name already used")
        self.setAccessibleName(self.windowTitle())
        self.choice: Resolution | None = None
        text = QLabel(
            f"“{name}” is already in this folder. Keep both (the new one gets a number), "
            "replace it (a folder is merged into the one there), or skip it?"
        )
        text.setWordWrap(True)
        self.setAccessibleDescription(text.text())
        rest = "the other item" if others == 1 else f"the other {others} items"
        self.for_all = QCheckBox(f"Do the &same for {rest} with names already used")
        self.for_all.setVisible(others > 0)
        buttons = QDialogButtonBox()
        self._keep = self._button(buttons, self.KEEP_BOTH, Resolution.KEEP_BOTH)
        self._button(buttons, self.REPLACE, Resolution.REPLACE)
        self._button(buttons, self.SKIP, Resolution.SKIP)
        cancel = buttons.addButton(self.CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setAutoDefault(False)
        buttons.rejected.connect(self.reject)
        self._keep.setDefault(True)
        layout = _framed(self)
        layout.addWidget(text)
        layout.addWidget(self.for_all)
        layout.addWidget(buttons)

    def _button(self, buttons: QDialogButtonBox, text: str, choice: Resolution) -> QPushButton:
        button = buttons.addButton(text, QDialogButtonBox.ButtonRole.ActionRole)
        button.setAutoDefault(False)
        button.clicked.connect(lambda: self._chosen(choice))
        return button

    def _chosen(self, choice: Resolution) -> None:
        self.choice = choice
        self.accept()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._keep.setFocus()


class UploadProgressDialog(QDialog):
    """Not modal: the rest of the app works meanwhile. Closing it, Escape and the button all
    mean "cancel"; it hides only once the upload has actually stopped (`finish`)."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Upload")
        self.setAccessibleName(self.windowTitle())
        self._total = 0
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._bar = QProgressBar()
        self._bar.setAccessibleName("Upload progress")
        self._bar.setTextVisible(False)  # a 6 px bar can't hold text: it's in the line below
        self._amount = QLabel()
        self._amount.setProperty("textRole", "caption")
        self._cancel = QPushButton("&Cancel upload")
        self._cancel.clicked.connect(self._ask_to_cancel)
        layout = _framed(self)
        layout.addWidget(self._label)
        layout.addWidget(self._bar)
        layout.addWidget(self._amount)
        layout.addWidget(self._cancel)

    def checking(self) -> None:
        """Planning: looking through what was chosen (may take a while for big folders)."""
        self._label.setText("Checking the files and the space they need…")
        self._bar.setRange(0, 0)  # busy, no amount yet
        self._amount.clear()
        self._cancel.setEnabled(True)

    def start(self, total: int) -> None:
        self._total = total
        self._label.setText("Copying into your DropShare folder…")
        self._bar.setRange(0, 100)
        self.set_done(0)
        self._cancel.setEnabled(True)

    def set_done(self, done: int) -> None:
        percent = 100 if self._total <= 0 else min(100, done * 100 // self._total)
        self._bar.setValue(percent)
        amount = f"{messages.size_text(done)} of {messages.size_text(self._total)}"
        self._amount.setText(f"{amount} \N{MIDDLE DOT} {percent}%")
        self._bar.setAccessibleDescription(f"{percent} percent, {amount}")

    def cancelling(self) -> None:
        self._label.setText("Cancelling and taking back what was copied…")
        self._cancel.setEnabled(False)

    @property
    def percent(self) -> int:
        return self._bar.value()

    def finish(self) -> None:
        self.hide()

    def _ask_to_cancel(self) -> None:
        if self._cancel.isEnabled():
            self.cancel_requested.emit()

    def reject(self) -> None:
        """Escape: the same as Cancel (the dialog stays until the upload has stopped)."""
        self._ask_to_cancel()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        event.ignore()
        self._ask_to_cancel()
