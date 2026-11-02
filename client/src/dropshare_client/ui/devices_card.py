"""Settings' signed-in computers (FR-AUTH-07): the list, in its loading, error with Retry,
empty and populated states (NFR-UX-01), and signing another computer out. This computer is
signed out with Sign out instead, which asks about its files (OQ-45)."""

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView, QFrame, QHeaderView, QPushButton, QTableView

from dropshare_client.controllers import messages
from dropshare_client.controllers.devices_controller import DeviceOut, DevicesController
from dropshare_client.ui import theme
from dropshare_client.ui.cards import button_row, fill_card
from dropshare_client.ui.devices_model import DevicesModel
from dropshare_client.ui.forms import confirm
from dropshare_client.ui.stateful_view import StatefulView

TITLE = "Signed-in computers"


class DevicesCard(QFrame):
    revoke_started = Signal()  # a computer is being signed out: clear old messages

    def __init__(self, devices: DevicesController) -> None:
        super().__init__()
        self._devices = devices
        # Hook (tests replace it): ask before signing another computer out.
        self.confirm: Callable[[str, str, str], bool] = lambda title, text, action: confirm(
            self, title, text, action
        )
        self._model = DevicesModel()
        self._table = _table(self._model)
        self._view = StatefulView(self._table)
        self._revoke = QPushButton("Sign out se&lected computer…")
        refresh = QPushButton("Re&fresh")

        layout = fill_card(self, TITLE, [])
        layout.addWidget(self._view)
        layout.addLayout(button_row(self._revoke, refresh))

        self._revoke.clicked.connect(self._ask_revoke)
        refresh.clicked.connect(devices.refresh)
        self._view.retry_requested.connect(devices.refresh)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update())
        devices.loading.connect(self._loading)
        devices.loaded.connect(self._show)
        devices.failed.connect(self._view.show_error)
        devices.busy_changed.connect(lambda _: self._update())
        self._update()

    @property
    def state(self) -> str:
        return self._view.current_state()

    @property
    def table(self) -> QTableView:
        return self._table

    def _loading(self) -> None:
        self._view.show_loading(messages.LOADING_DEVICES, keep_content=False)

    def _show(self) -> None:
        devices = self._devices.devices()
        self._model.set_rows(devices)
        if devices:
            self._view.show_content()
        else:
            self._view.show_empty(messages.DEVICES_EMPTY)
        self._update()

    def _selected(self) -> DeviceOut | None:
        rows = self._table.selectionModel().selectedRows()
        return self._model.device_at(rows[0].row()) if rows else None

    def _update(self) -> None:
        device = self._selected()
        this_computer = device is not None and device.is_current
        self._revoke.setEnabled(device is not None and not this_computer and not self._devices.busy)
        why = messages.USE_SIGN_OUT_HERE if this_computer else ""
        self._revoke.setAccessibleDescription(why)
        self._revoke.setToolTip(why or "Sign the selected computer out of DropShare")

    def _ask_revoke(self) -> None:
        device = self._selected()
        if device is None or device.is_current or self._devices.busy:
            return
        text = messages.confirm_device_sign_out(device.name)
        if self.confirm("Sign out computer", text, "Sign out"):
            self.revoke_started.emit()
            self._devices.revoke(device)


def _table(model: DevicesModel) -> QTableView:
    table = QTableView()
    table.setObjectName("devicesTable")
    table.setAccessibleName(TITLE)
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setShowGrid(False)
    table.verticalHeader().hide()
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in (1, 2):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    table.setMinimumHeight(theme.DEVICES_TABLE_MIN_HEIGHT)
    return table
