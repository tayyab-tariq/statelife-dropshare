import os
import sys
from collections.abc import Callable

import pytest
from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget

# Headless Qt so tests run without a display (CI, SSH sessions). Visual checks on the Windows
# machine run the app itself, not these tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="needs Windows (run on the Windows test machine)")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip)


def _spoken_name(widget: QWidget) -> str:
    """The name assistive technology announces: explicit accessible name, else visible text."""
    if widget.accessibleName():
        return widget.accessibleName()
    if isinstance(widget, QLabel | QAbstractButton):
        return widget.text().replace("&", "")
    return ""


type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]


@pytest.fixture
def find_by_name() -> FindByName:
    """Find exactly one child widget by the name a screen reader would use (ui.md §8)."""

    def find(root: QWidget, widget_type: type[QWidget], name: str) -> QWidget:
        matches = [w for w in root.findChildren(widget_type) if _spoken_name(w) == name]
        assert len(matches) == 1, f"expected one {widget_type.__name__} named {name!r}"
        return matches[0]

    return find
