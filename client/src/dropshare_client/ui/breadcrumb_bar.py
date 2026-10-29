"""Where the user is in My files (FR-FILE-01): Up, the folders above as buttons, and the current
folder as the title. Long paths keep the first and the last two folders and fold the middle
into "…", with the whole path in the tooltip and for screen readers; names never become
keyboard mnemonics ("R&D" stays "R&D")."""

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from dropshare_client.ui import theme

_SEPARATOR = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
_FOLDED = "\N{HORIZONTAL ELLIPSIS}"
_SHOWN_BEFORE_CURRENT = 2  # parents shown next to the current folder when folded


def _escaped(name: str) -> str:
    return name.replace("&", "&&")


class BreadcrumbBar(QWidget):
    up_requested = Signal()
    crumb_chosen = Signal(int)  # index in the full list of names

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._up = QPushButton("&Up")
        self._up.setAccessibleName("Up one folder")
        self._up.setToolTip("Go to the folder above (Alt+Up)")
        self._up.setProperty("variant", "subtle")
        self._up.setIconSize(QSize(theme.TOOLBAR_ICON_SIZE, theme.TOOLBAR_ICON_SIZE))
        self._up.clicked.connect(self.up_requested)
        self._crumbs = QHBoxLayout()
        self._crumbs.setContentsMargins(0, 0, 0, 0)
        self._crumbs.setSpacing(theme.SPACE.xs)
        self._buttons: list[QPushButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE.sm)
        layout.addWidget(self._up)
        layout.addLayout(self._crumbs, stretch=1)
        self.retint()

    def retint(self) -> None:
        self._up.setIcon(theme.icon("arrow_up"))

    def focus_chain(self) -> list[QWidget]:
        """Its focusable widgets in the order they appear."""
        return [self._up, *self._buttons]

    def show_path(self, names: list[str]) -> None:
        focused = QApplication.focusWidget()
        had_focus = focused is not None and self.isAncestorOf(focused)
        while self._crumbs.count():
            item = self._crumbs.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._buttons = []
        self._up.setEnabled(len(names) > 1)
        whole = f" {_SEPARATOR} ".join(names)
        self.setToolTip(whole)
        self.setAccessibleDescription(f"Folder: {whole}")
        shown = list(enumerate(names))
        if len(names) > _SHOWN_BEFORE_CURRENT + 2:
            shown = [shown[0], (-1, _FOLDED), *shown[-(_SHOWN_BEFORE_CURRENT + 1) :]]
        for position, (index, name) in enumerate(shown):
            if position:
                separator = QLabel(_SEPARATOR)
                separator.setProperty("textRole", "caption")
                separator.setAccessibleName("")
                self._crumbs.addWidget(separator)
            if index == len(names) - 1:
                current = QLabel(name)
                current.setProperty("textRole", "title")
                current.setAccessibleName(f"Current folder: {name}")
                current.setToolTip(whole)
                self._crumbs.addWidget(current, stretch=0)
            elif index < 0:
                folded = QLabel(_FOLDED)
                folded.setToolTip(whole)
                folded.setAccessibleName("More folders")
                self._crumbs.addWidget(folded)
            else:
                button = QPushButton(_escaped(name))
                button.setProperty("variant", "subtle")
                button.setAccessibleName(f"Go to {name}")
                button.clicked.connect(lambda _=False, i=index: self.crumb_chosen.emit(i))
                self._crumbs.addWidget(button)
                self._buttons.append(button)
        self._crumbs.addStretch()
        if had_focus:
            self._up.setFocus()  # the button that had focus is gone
