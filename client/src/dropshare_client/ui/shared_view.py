"""Shared with me (FR-FILE-01): a placeholder until sharing (roadmap M8) explains what will
appear here."""

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from dropshare_client.ui import theme


class SharedWithMeView(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        space = theme.SPACE
        title = QLabel("Shared with me")
        title.setProperty("textRole", "title")
        note = QLabel("Folders and files other people share with you will appear here.")
        note.setObjectName("shared_placeholder")
        note.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.lg)
        layout.addWidget(title)
        layout.addWidget(note)
        layout.addStretch()
