from collections.abc import Callable

import pytest
from client_fakes import Client
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.ui import theme
from dropshare_client.ui.sign_in_dialog import SignInDialog

type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]


@pytest.mark.parametrize("dark", [False, True])
def test_text_on_brand_fills_meets_wcag_aa(dark: bool) -> None:
    t = theme.tokens_for(dark)
    for fill in (t.primary, t.primary_hover, t.primary_pressed):
        assert theme.contrast_ratio(t.on_primary, fill) >= 4.5, fill.name()


@pytest.mark.parametrize("dark", [False, True])
def test_body_secondary_and_error_text_meet_wcag_aa(dark: bool) -> None:
    t = theme.tokens_for(dark)
    for text in (t.text_primary, t.text_secondary, t.danger):
        assert theme.contrast_ratio(text, t.bg_surface) >= 4.5, text.name()


def test_stylesheet_follows_the_scheme_and_scales_from_the_body_size() -> None:
    light = theme.build_stylesheet(theme.tokens_for(False), 10.0)
    dark = theme.build_stylesheet(theme.tokens_for(True), 10.0)

    assert light != dark
    assert "font-size: 16.0pt" in light  # title = body x 1.6
    assert "#0b6e4f" in light  # the brand primary, so replacing _BRAND carries through


def test_logo_keeps_its_aspect_and_is_crisp_on_hidpi(qapp: QApplication) -> None:
    pixmap = theme.logo(64, 2.0)

    assert pixmap.deviceIndependentSize().height() == 64
    assert pixmap.deviceIndependentSize().width() > 64  # the full logo is wider than tall
    assert pixmap.height() == 128
    assert theme.logo_mark(24, 1.5).width() == 36


def test_high_contrast_leaves_the_system_look(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    native, font = qapp.style().name(), qapp.font()
    applied = theme.Theme(qapp)
    assert qapp.styleSheet()
    assert qapp.palette().resolveMask() != QPalette().resolveMask()

    monkeypatch.setattr(theme, "_high_contrast", lambda: True)
    applied.apply()

    assert qapp.styleSheet() == ""
    assert qapp.style().name() == native
    assert qapp.palette().resolveMask() == QPalette().resolveMask()
    assert qapp.font() == font
    applied.deleteLater()  # stays unthemed, as the other tests expect


def test_sign_in_dialog_has_one_primary_action(
    qtbot: QtBot, find_by_name: FindByName, make_client: Callable[[], Client]
) -> None:
    dialog = SignInDialog(make_client().session)
    qtbot.addWidget(dialog)

    primaries = [b for b in dialog.findChildren(QPushButton) if b.property("variant") == "primary"]
    assert [b.text() for b in primaries] == ["Sign in"]
    sign_in = find_by_name(dialog, QPushButton, "Sign in")
    assert isinstance(sign_in, QPushButton) and sign_in.isDefault()
    assert find_by_name(dialog, QLineEdit, "Username or email")
