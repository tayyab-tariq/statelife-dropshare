"""Small building blocks shared by the dialogs."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QFormLayout, QLabel, QLineEdit, QMessageBox, QWidget


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


def show_message(label: QLabel, text: str) -> None:
    """Show (or hide, for "") a message and have screen readers announce it."""
    label.setText(text)
    label.setVisible(bool(text))
    if text:
        QAccessible.updateAccessibility(QAccessibleEvent(label, QAccessible.Event.Alert))


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
