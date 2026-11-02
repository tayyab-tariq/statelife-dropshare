"""A small window while a remembered update is checked at start (FR-UPD-03): the check runs
before the event loop, so this is painted once and then left as it is until it closes."""

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from dropshare_client.controllers import messages
from dropshare_client.ui import theme


class InstallingNotice(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("StateLife DropShare")
        self.setAccessibleName(self.windowTitle())
        text = QLabel(messages.INSTALLING_UPDATE)
        text.setWordWrap(True)
        self.setAccessibleDescription(text.text())
        layout = QVBoxLayout(self)
        space = theme.SPACE
        layout.setContentsMargins(space.xl, space.xl, space.xl, space.xl)
        layout.addWidget(text)
        self.show()
        QApplication.processEvents()  # painted now: nothing runs the event loop until it closes
