from collections.abc import Callable

from PySide6.QtWidgets import QLabel, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.ui.main_window import MainWindow


def test_main_window_shows_product_name(
    qtbot: QtBot, find_by_name: Callable[[QWidget, type[QWidget], str], QWidget]
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "StateLife DropShare"
    assert window.accessibleName() == "DropShare"
    heading = find_by_name(window, QLabel, "StateLife DropShare")
    assert heading.isVisible()
