"""The Share dialog (FR-SHARE-01/03/04/07/08): find people and groups, pick what they can do,
see everyone with access, change or remove them, and copy the item's link. Only the owner may
change who has access; anyone with access sees the list and can copy the link."""

import uuid
from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.share_controller import (
    SEARCH_MIN_LENGTH,
    Grantee,
    GranteeKind,
    MemberOut,
    ShareController,
    ShareRole,
    SharingOut,
)
from dropshare_client.ui import theme
from dropshare_client.ui.forms import announce, confirm, message_label, show_message, status_label
from dropshare_client.ui.stateful_view import StatefulView

_ROLES = ((ShareRole.EDITOR, "Can edit"), (ShareRole.VIEWER, "Can view"))
_ROLE_TEXT: dict[str, str] = dict(_ROLES)  # by value: a role from a newer server is shown as is
_DATA = Qt.ItemDataRole.UserRole
_RESULT_ROWS = 5  # search results shown without scrolling


def _role_of(box: QComboBox) -> ShareRole:
    """Qt hands item data back as a plain string; the role is rebuilt from it."""
    return ShareRole(str(box.currentData()))


class _NoWheel(QObject):
    """A role picker changes access at once: scrolling past it mustn't."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        return event.type() == QEvent.Type.Wheel


def _role_box(name: str) -> QComboBox:
    box = QComboBox()
    box.setAccessibleName(name)
    box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    for role, text in _ROLES:
        box.addItem(text, role)
    return box


def _labelled(text: str, field: QWidget) -> QLabel:
    """A visible label that names the field and focuses it with its Alt key (NFR-UX-02)."""
    label = QLabel(text)
    label.setBuddy(field)
    return label


class ShareDialog(QDialog):
    def __init__(self, controller: ShareController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._members: list[MemberOut] = []
        self._can_manage = False
        self._loaded = False
        # Hook (tests replace it): ask before access is taken away.
        self.confirm: Callable[[str, str, str], bool] = lambda title, text, action: confirm(
            self, title, text, action
        )
        self.setMinimumWidth(theme.DIALOG_WIDTH)
        space = theme.SPACE

        self._title = QLabel()
        self._title.setProperty("textRole", "title")
        self._title.setWordWrap(True)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Name or email")
        self._search.setAccessibleName("Find people or groups")
        self._search.setClearButtonEnabled(True)
        self._role = _role_box("What they can do")
        self._results = QListWidget()
        self._results.setAccessibleName("People and groups found")
        rows = self._results.fontMetrics().height() + space.sm
        self._results.setMaximumHeight(rows * _RESULT_ROWS + 2 * space.xs)
        self._share = QPushButton("&Share")
        self._share.setProperty("variant", "primary")
        self._share.setEnabled(False)
        find_row = QHBoxLayout()
        find_row.addWidget(self._search, stretch=1)
        find_row.addWidget(self._role)
        find_row.addWidget(self._share)
        self._finding = QWidget()
        finding = QVBoxLayout(self._finding)
        finding.setContentsMargins(0, 0, 0, 0)
        finding.addWidget(_labelled("&Add people or groups", self._search))
        finding.addLayout(find_row)
        finding.addWidget(self._results)

        self._owner_only = QLabel("Only the owner can change who has access.")
        self._owner_only.setWordWrap(True)
        self._owner_only.setProperty("textRole", "caption")

        self._list = QListWidget()
        self._list.setAccessibleName("People with access")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        heading = _labelled("&People with access", self._list)
        heading.setProperty("textRole", "subtitle")
        self._state = StatefulView(self._list)
        self._change = _role_box("Change what they can do")
        self._no_wheel = _NoWheel(self)
        self._change.installEventFilter(self._no_wheel)
        self._remove = QPushButton("&Remove access…")
        self._remove.setProperty("variant", "danger")
        manage_row = QHBoxLayout()
        manage_row.setContentsMargins(0, 0, 0, 0)
        manage_row.addWidget(_labelled("Can:", self._change))
        manage_row.addWidget(self._change)
        manage_row.addWidget(self._remove)
        manage_row.addStretch()
        self._manage = QWidget()
        self._manage.setLayout(manage_row)

        self._error = message_label("shareError")
        self._status = status_label("shareStatus")
        self._copy_link = QPushButton("Copy &link")
        self._copy_link.setEnabled(False)  # until the item is known (S9)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self._copy_link, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xl, space.xl, space.xl, space.xl)
        layout.setSpacing(space.md)
        layout.addWidget(self._title)
        layout.addWidget(self._finding)
        layout.addWidget(self._owner_only)
        layout.addWidget(heading)
        layout.addWidget(self._state, stretch=1)
        layout.addWidget(self._manage)
        layout.addWidget(self._error)
        layout.addWidget(self._status)
        layout.addWidget(buttons)

        self._search.textChanged.connect(controller.search)
        self._results.currentRowChanged.connect(lambda _: self._update())
        self._results.itemActivated.connect(lambda _: self._add())
        self._share.clicked.connect(self._add)
        self._list.currentRowChanged.connect(lambda _: self._member_chosen())
        self._change.activated.connect(lambda _: self._change_role())
        self._remove.clicked.connect(self._remove_member)
        self._copy_link.clicked.connect(self._copy)
        self._state.retry_requested.connect(controller.reload)
        controller.loading.connect(self._loading)
        controller.loaded.connect(self._show)
        controller.failed.connect(self._failed)
        controller.found.connect(self._show_found)
        controller.searching.connect(self._show_searching)
        controller.busy_changed.connect(lambda _: self._update())
        controller.changed.connect(self._announce)
        controller.change_failed.connect(lambda message: show_message(self._error, message))
        self._update()

    def open_for(self, node_id: uuid.UUID, name: str) -> None:
        """Show the dialog for one item. Nothing of the previous item stays: its list, its
        rights and its link are gone before this one's load (B1)."""
        self.setWindowTitle(f"Share “{name}”")
        self.setAccessibleName(self.windowTitle())
        self._title.setText(self.windowTitle())
        self._members, self._can_manage, self._loaded = [], False, False
        self._list.clear()
        self._search.clear()
        self._results.clear()
        announce(self._status, "")
        show_message(self._error, "")
        self._update()
        self._controller.open(node_id)
        self.show()
        self.raise_()
        self.activateWindow()

    # --- showing ---------------------------------------------------------------------------

    def _loading(self, keep: bool) -> None:
        self._state.show_loading("Loading who has access…", keep_content=keep)

    def _failed(self, message: str) -> None:
        self._loaded = False
        self._update()
        self._state.show_error(message)

    def _show(self, sharing: SharingOut) -> None:
        chosen = self._chosen_member()
        self._can_manage = sharing.can_manage
        self._members = list(sharing.members)
        self._loaded = True
        self._list.clear()
        owner_text = f"{sharing.owner.display_name} ({sharing.owner.email}) · Owner"
        owner = QListWidgetItem(owner_text)
        owner.setData(Qt.ItemDataRole.AccessibleTextRole, owner_text.replace(" · ", ", "))
        owner.setFlags(owner.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self._list.addItem(owner)
        for member in self._members:
            who = member.name if member.email is None else f"{member.name} ({member.email})"
            kind = "group" if member.kind == GranteeKind.GROUP else "person"
            role = _ROLE_TEXT.get(member.role, str(member.role))
            item = QListWidgetItem(f"{who} · {role}")
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{who}, {kind}, {role}")
            item.setData(_DATA, member.id)
            self._list.addItem(item)
            if chosen is not None and member.id == chosen.id:  # keep the place after a change
                self._list.setCurrentItem(item)
        self._state.show_content()
        self._update()

    def _show_found(self, hits: list[Grantee]) -> None:
        self._results.clear()
        for hit in hits:
            kind = "Group" if hit.kind == GranteeKind.GROUP else "Person"
            item = QListWidgetItem(f"{hit.name} · {hit.detail}")
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{kind}: {hit.name}, {hit.detail}")
            item.setData(_DATA, hit)
            self._results.addItem(item)
        if hits:
            self._results.setCurrentRow(0)
        elif len(self._search.text().strip()) >= SEARCH_MIN_LENGTH and not self._error.isVisible():
            announce(self._status, "Nobody found. Check the spelling, or try an email address.")
        self._update()

    def _show_searching(self, searching: bool) -> None:
        if searching:
            self._status.setText("Searching…")
        elif self._status.text() == "Searching…":
            self._status.clear()

    def _announce(self, text: str) -> None:
        show_message(self._error, "")
        announce(self._status, text)
        self._search.clear()

    # --- what can be done now --------------------------------------------------------------

    def _chosen_hit(self) -> Grantee | None:
        item = self._results.currentItem()
        hit = None if item is None else item.data(_DATA)
        return hit if isinstance(hit, Grantee) else None

    def _chosen_member(self) -> MemberOut | None:
        item = self._list.currentItem()
        member_id = None if item is None else item.data(_DATA)
        return next((m for m in self._members if m.id == member_id), None)

    def _update(self) -> None:
        idle = not self._controller.busy
        manage = self._loaded and self._can_manage
        self._finding.setVisible(manage)
        self._manage.setVisible(manage)
        self._owner_only.setVisible(self._loaded and not self._can_manage)
        self._copy_link.setEnabled(self._loaded)
        self._share.setEnabled(idle and manage and self._chosen_hit() is not None)
        member = self._chosen_member()
        self._change.setEnabled(idle and manage and member is not None)
        self._remove.setEnabled(idle and manage and member is not None)

    def _member_chosen(self) -> None:
        member = self._chosen_member()
        if member is not None:
            index = self._change.findData(member.role)
            self._change.setCurrentIndex(max(index, 0))
        self._update()

    # --- changes -------------------------------------------------------------------------------

    def _add(self) -> None:
        hit = self._chosen_hit()
        if hit is not None and self._share.isEnabled():
            self._controller.add(hit, _role_of(self._role))

    def _change_role(self) -> None:
        member, role = self._chosen_member(), _role_of(self._change)
        if member is not None and role != member.role:
            self._controller.change_role(member, role)
            self._list.setFocus()

    def _remove_member(self) -> None:
        member = self._chosen_member()
        if member is None:
            return
        if self.confirm(
            "Remove access",
            f"{member.name} will no longer have access. If they added this folder to their "
            "DropShare, it is removed from their computers.",
            "Remove access",
        ):
            self._controller.remove(member)
            self._list.setFocus()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._controller.link())
        announce(self._status, messages.LINK_COPIED)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._search.setFocus()
