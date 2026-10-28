"""The UI side of an upload from the app (FR-FILE-02): choosing what to add, asking about names
already used, showing progress, and confirming a cancel. Kept out of BrowserView, which only
starts uploads and shows the outcome.

The dialogs go through replaceable hooks (`choose_files`, `choose_folder`, `ask_clash`,
`confirm_cancel`), so tests answer them without opening windows.
"""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QWidget

from dropshare_client.controllers.file_actions_controller import Item
from dropshare_client.controllers.upload_controller import Resolution, UploadController
from dropshare_client.ui.upload_dialogs import CANCEL_UPLOAD, ClashDialog, UploadProgressDialog

type ChooseFiles = Callable[[], list[Path]]
type ChooseFolder = Callable[[], Path | None]
# A name already used -> (choice, the same for the rest) or None to cancel the upload.
type AskClash = Callable[[str, int], tuple[Resolution, bool] | None]
type ConfirmCancel = Callable[[], bool]


class UploadFlow(QObject):
    succeeded = Signal(str)  # for the browser's status line
    failed = Signal(str)

    def __init__(self, uploads: UploadController, parent: QWidget) -> None:
        super().__init__(parent)
        self._uploads = uploads
        self._parent = parent
        self.choose_files: ChooseFiles = self._choose_files
        self.choose_folder: ChooseFolder = self._choose_folder
        self.ask_clash: AskClash = self._ask_clash
        self.confirm_cancel: ConfirmCancel = self._confirm_cancel
        self._progress: UploadProgressDialog | None = None
        uploads.planning.connect(self._planning)
        uploads.clashes_found.connect(self._ask_clashes)
        uploads.started.connect(self._started)
        uploads.progressed.connect(self._progressed)
        uploads.succeeded.connect(self._ended)
        uploads.cancelled.connect(self._ended)
        uploads.failed.connect(self._failed)

    @property
    def busy(self) -> bool:
        return self._uploads.busy

    @property
    def progress(self) -> UploadProgressDialog | None:
        return self._progress

    def upload_files(self, folder: Item) -> None:
        self.upload(self.choose_files(), folder)

    def upload_folder(self, folder: Item) -> None:
        chosen = self.choose_folder()
        if chosen is not None:
            self.upload([chosen], folder)

    def upload(self, paths: list[Path], folder: Item) -> None:
        if paths:
            self._uploads.upload(paths, folder.path)

    # --- the controller's questions and reports -----------------------------------------

    def _dialog(self) -> UploadProgressDialog:
        if self._progress is None:
            self._progress = UploadProgressDialog(self._parent)
            self._progress.cancel_requested.connect(self._cancel)
        return self._progress

    def _planning(self) -> None:
        dialog = self._dialog()
        dialog.checking()
        dialog.show()

    def _ask_clashes(self, names: list[str]) -> None:
        """One question per name already used, unless the user applies an answer to all."""
        answers: dict[str, Resolution] = {}
        for position, name in enumerate(names):
            answer = self.ask_clash(name, len(names) - position - 1)
            if answer is None:
                self._uploads.cancel()
                return
            choice, for_all = answer
            answers[name] = choice
            if for_all:
                answers.update(dict.fromkeys(names[position + 1 :], choice))
                break
        self._uploads.choose(answers)

    def _started(self, total: int) -> None:
        dialog = self._dialog()
        dialog.start(total)
        dialog.show()

    def _progressed(self, done: int) -> None:
        if self._progress is not None:
            self._progress.set_done(done)

    def _cancel(self) -> None:
        if self.confirm_cancel():
            if self._progress is not None:
                self._progress.cancelling()
            self._uploads.cancel()

    def _ended(self, message: str) -> None:
        if self._progress is not None:
            self._progress.finish()
        self.succeeded.emit(message)

    def _failed(self, message: str) -> None:
        if self._progress is not None:
            self._progress.finish()
        self.failed.emit(message)

    # --- default dialogs -----------------------------------------------------------------

    def _choose_files(self) -> list[Path]:
        chosen, _ = QFileDialog.getOpenFileNames(self._parent, "Upload files")
        return [Path(path) for path in chosen]

    def _choose_folder(self) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self._parent, "Upload a folder")
        return Path(chosen) if chosen else None

    def _ask_clash(self, name: str, others: int) -> tuple[Resolution, bool] | None:
        dialog = ClashDialog(name, others, parent=self._parent)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.choice is None:
            return None
        return dialog.choice, dialog.for_all.isChecked()

    def _confirm_cancel(self) -> bool:
        answer = QMessageBox.question(
            self._parent,
            "Cancel upload",
            CANCEL_UPLOAD,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
