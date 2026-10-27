"""Small building blocks shared by the dialogs."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import QFormLayout, QLabel, QLineEdit, QWidget


def message_label(object_name: str) -> QLabel:
    """Where a view says what went wrong; hidden until there's something to say.

    Deliberately no accessible name: a label's name *replaces* its text for screen readers,
    and the text is what must be read. Tests find it by object name instead.
    """
    label = QLabel()
    label.setObjectName(object_name)
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
