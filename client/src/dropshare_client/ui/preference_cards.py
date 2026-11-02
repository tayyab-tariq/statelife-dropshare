"""Settings' preferences, each saved as it changes (FR-CLIENT-04): starting at Windows sign-in
(OQ-44), which notifications show (FR-CLIENT-02), and files transferred at once (OQ-39)."""

from PySide6.QtWidgets import QCheckBox, QFormLayout, QFrame, QLabel, QSpinBox, QVBoxLayout, QWidget

from dropshare_client.controllers import messages
from dropshare_client.controllers.settings_controller import (
    MAX_TRANSFERS_LIMIT,
    Notice,
    SettingsController,
)
from dropshare_client.ui import theme
from dropshare_client.ui.cards import card
from dropshare_client.ui.forms import add_field

_NOTIFICATIONS = (
    (Notice.NEW_SHARE, "Something is shared with me"),
    (Notice.CONFLICTS, "A file was changed in two places (conflicts)"),
    (Notice.CANT_SYNC, "Items can't be synced"),
    (Notice.QUOTA, "My storage is almost full or full"),
)


class PreferenceCards(QWidget):
    """The Startup, Notifications and Transfers cards, showing what is saved."""

    def __init__(self, settings: SettingsController) -> None:
        super().__init__()
        self._settings = settings
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE.lg)
        for section in (self._startup_card(), self._notifications_card(), self._transfers_card()):
            layout.addWidget(section)
        settings.changed.connect(self._show)
        self._show()

    def _startup_card(self) -> QFrame:
        self._autostart = QCheckBox("Start DropShare when I sign in to &Windows")
        self._autostart_note = QLabel(messages.AUTOSTART_UNAVAILABLE)
        self._autostart_note.setProperty("textRole", "caption")
        startup, layout = card("Startup", [])
        layout.addWidget(self._autostart)
        layout.addWidget(self._autostart_note)
        self._autostart.toggled.connect(self._settings.set_autostart)
        return startup

    def _notifications_card(self) -> QFrame:
        notifications, layout = card("Notify me when", [])
        self._boxes: dict[Notice, QCheckBox] = {}
        for kind, text in _NOTIFICATIONS:
            box = QCheckBox(text)
            box.toggled.connect(lambda on, kind=kind: self._settings.set_notification(kind, on))
            layout.addWidget(box)
            self._boxes[kind] = box
        return notifications

    def _transfers_card(self) -> QFrame:
        self._max_transfers = QSpinBox()
        self._max_transfers.setRange(1, MAX_TRANSFERS_LIMIT)
        self._max_transfers.setKeyboardTracking(False)  # saved once, not per keystroke
        transfers, layout = card("Transfers", [])
        form = QFormLayout()
        # A number of one digit: as wide as it needs, not the whole card.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        add_field(form, "Files transferred at &once", self._max_transfers)
        layout.addLayout(form)
        self._max_transfers.valueChanged.connect(self._settings.set_max_transfers)
        return transfers

    def _show(self) -> None:
        """The controls as saved, without saving again (also after a save failed)."""
        settings = self._settings
        controls: list[QWidget] = [self._autostart, self._max_transfers, *self._boxes.values()]
        for control in controls:
            control.blockSignals(True)
        self._autostart.setChecked(settings.autostart_available and settings.autostart)
        self._autostart.setEnabled(settings.autostart_available)
        self._autostart_note.setVisible(not settings.autostart_available)
        for kind, box in self._boxes.items():
            box.setChecked(bool(getattr(settings.notifications, kind.value)))
        self._max_transfers.setValue(settings.max_transfers)
        for control in controls:
            control.blockSignals(False)
