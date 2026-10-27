"""Main window, four-state view and tray (NFR-UX-01/02, FR-CLIENT-01)."""

import uuid
from collections.abc import Callable

from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.ui.main_window import MainWindow
from dropshare_client.ui.stateful_view import StatefulView
from dropshare_client.ui.theme import TrayState, status_icon
from dropshare_client.ui.tray import Tray
from dropshare_shared.schemas.users import UserOut, UserRole

type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]

_USER = UserOut(
    id=uuid.uuid4(),
    username="alice",
    email="alice@example.com",
    display_name="Alice",
    role=UserRole.USER,
    quota_bytes=1,
)


def test_window_shows_account_folder_and_status(qtbot: QtBot, find_by_name: FindByName) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    window.show_account(_USER, "https://dropshare.example.com", "C:\\Sync", "Up to date")

    assert window.windowTitle() == "StateLife DropShare"
    # Screen readers hear each value together with what it is (NFR-UX-02).
    for name, text in (
        ("Signed in as: Alice (alice)", "Alice (alice)"),
        ("Server: https://dropshare.example.com", "https://dropshare.example.com"),
        ("Sync folder: C:\\Sync", "C:\\Sync"),
        ("Status: Up to date", "Up to date"),
    ):
        label = find_by_name(window, QLabel, name)
        assert isinstance(label, QLabel) and label.text() == text
    assert find_by_name(window, QPushButton, "Open sync folder").isVisible()


def test_message_labels_are_read_by_their_text(qtbot: QtBot) -> None:
    """An accessible name would replace the message for screen readers."""
    window = MainWindow()
    qtbot.addWidget(window)

    window.show_error("Can't reach the DropShare server.")

    error = window.findChild(QLabel, "error_message")
    assert isinstance(error, QLabel)
    assert error.accessibleName() == ""
    assert error.text() == "Can't reach the DropShare server."


def test_without_a_sync_folder_the_window_offers_to_choose_one(
    qtbot: QtBot, find_by_name: FindByName
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    window.show_account(_USER, "https://dropshare.example.com", None, "Up to date")

    assert window.view.current_state() == "empty"
    # Account actions stay reachable when there's no folder yet.
    assert find_by_name(window, QPushButton, "Sign out…").isVisible()
    with qtbot.waitSignal(window.choose_folder_requested):
        button = find_by_name(window, QPushButton, "Choose sync folder…")
        assert isinstance(button, QPushButton)
        button.click()


def test_closing_the_window_hides_it_to_the_tray(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    window.close()
    hidden = not window.isVisible()
    window.show()
    window.quit()  # only Quit really closes it

    assert hidden
    assert not window.isVisible()


def test_without_a_tray_closing_the_window_quits(qtbot: QtBot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_hide_on_close(False)
    window.show()

    with qtbot.waitSignal(window.quit_requested):
        window.close()


def test_stateful_view_states(qtbot: QtBot, find_by_name: FindByName) -> None:
    content = QLabel("Loaded")
    view = StatefulView(content)
    qtbot.addWidget(view)
    view.show()

    view.show_loading()
    assert view.current_state() == "loading"
    view.show_error("Couldn't load.")
    assert view.current_state() == "error"
    with qtbot.waitSignal(view.retry_requested):
        retry = find_by_name(view, QPushButton, "Try again")
        assert isinstance(retry, QPushButton)
        retry.click()
    view.show_content()
    view.show_loading()  # refreshing keeps the loaded content on screen
    assert view.current_state() == "content"


def test_tray_menu_and_states(qtbot: QtBot) -> None:
    tray = Tray()
    texts = [action.text() for action in tray._menu.actions() if not action.isSeparator()]

    assert texts == [
        "Open sync folder",
        "Open DropShare",
        "Recent activity",
        "Pause syncing",
        "Resume syncing",
        "Settings",
        "Quit",
    ]
    assert tray.tooltip() == "StateLife DropShare: Signed out"
    assert not tray.open_folder.isEnabled()

    tray.set_state(TrayState.UP_TO_DATE)
    assert tray.open_folder.isEnabled() and tray.pause.isVisible()
    with qtbot.waitSignal(tray.pause_toggled) as paused:
        tray.pause.trigger()
    assert paused.args == [True]

    tray.set_paused(True)
    assert tray.resume.isVisible() and not tray.pause.isVisible()
    tray.set_state(TrayState.OFFLINE, "retrying")
    assert tray.tooltip() == "StateLife DropShare: Offline (retrying)"


def test_every_tray_state_has_its_own_icon() -> None:
    """States differ by glyph, not just colour (NFR-UX-02)."""
    images = {state: status_icon(state).pixmap(32, 32).toImage() for state in TrayState}

    for state, image in images.items():
        others = [other for other in TrayState if other is not state]
        assert all(image != images[other] for other in others), state
