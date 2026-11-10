"""Small building blocks shared by the dialogs."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)


def message_label(object_name: str) -> QLabel:
    """Where a view says what went wrong; hidden until there's something to say.

    Deliberately no accessible name: a label's name *replaces* its text for screen readers,
    and the text is what must be read. Tests find it by object name instead.
    """
    label = QLabel()
    label.setObjectName(object_name)
    label.setProperty("textRole", "error")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.hide()
    return label


_FROM_RELOAD = "fromReload"  # the label ends with a failed reload's message
_KEPT = "keptMessage"  # what it showed before that, still true


def show_message(
    label: QLabel, text: str, *, from_reload: bool = False, alert: bool = True
) -> None:
    """Show (or hide, for "") a message and have screen readers announce it (unless `alert`
    is False). `from_reload`: it says a reload failed. A message already shown (an action
    that failed) stays in front of it, and the next reload that works takes away only the
    reload's part (`clear_reload_message`); any other message replaces the whole text."""
    kept = ""
    if from_reload and text:
        kept = str(label.property(_KEPT) or "") if label.property(_FROM_RELOAD) else label.text()
    label.setProperty(_FROM_RELOAD, from_reload and bool(text))
    label.setProperty(_KEPT, kept)
    shown = kept if kept == text else "\n\n".join(part for part in (kept, text) if part)
    label.setText(shown)
    label.setVisible(bool(shown))
    if shown and alert:
        QAccessible.updateAccessibility(QAccessibleEvent(label, QAccessible.Event.Alert))


def clear_reload_message(label: QLabel) -> None:
    """A reload worked: take away a failed reload's message, leaving what was shown before it
    (not announced again: nothing new happened to it)."""
    if label.property(_FROM_RELOAD):
        show_message(label, str(label.property(_KEPT) or ""), alert=False)


def add_field(form: QFormLayout, text: str, field: QWidget) -> None:
    """A row whose label is the field's buddy: Alt+mnemonic focuses it, and screen readers read
    the label as the field's name."""
    label = QLabel(text)
    label.setBuddy(field)
    field.setAccessibleName(text.replace("&", ""))
    form.addRow(label, field)


def password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    return field


def status_label(object_name: str) -> QLabel:
    """Where a view confirms what it did. No accessible name, for the same reason as
    `message_label`: `announce` has screen readers read its text."""
    label = QLabel()
    label.setObjectName(object_name)
    label.setWordWrap(True)
    return label


def announce(label: QLabel, text: str) -> None:
    """Show a confirmation (or clear it, for "") and have screen readers read it out."""
    label.setText(text)
    if text:
        QAccessible.updateAccessibility(QAccessibleEvent(label, QAccessible.Event.Alert))


def confirm(parent: QWidget, title: str, text: str, action: str) -> bool:
    """Ask before something that can't simply be undone (ui.md §5): the action's own words on
    the button, Cancel the default."""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, parent=parent)
    box.setAccessibleName(title)
    go = box.addButton(action, QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is go


def set_available(button: QAbstractButton, available: bool, why: str) -> None:
    """Enable or disable, with the reason (when off) as tooltip and screen-reader
    description, so a disabled action says why (ui.md §5)."""
    button.setEnabled(available)
    button.setToolTip(why)
    button.setAccessibleDescription(why)
