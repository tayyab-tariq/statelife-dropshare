"""What installer builds bake in: the server address and the update publisher (FR-CLIENT-05,
FR-UPD-03)."""

import json
import sys
from pathlib import Path

import pytest

from dropshare_client import platform
from dropshare_client.build_info import DEVELOPMENT, BuildInfo, BuildInfoError, load
from dropshare_shared.signing import UnavailableVerifier


def _write(path: Path, **values: object) -> Path:
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_from_source_the_development_values_apply(tmp_path: Path) -> None:
    assert load(tmp_path / "missing.json", frozen=False) == DEVELOPMENT
    assert DEVELOPMENT.publisher == ""  # so no update is ever installed from source


def test_an_installed_build_reads_its_values(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "build_info.json",
        server_url="https://dropshare.example.com/",
        publisher="StateLife",
    )

    assert load(path, frozen=True) == BuildInfo(
        server_url="https://dropshare.example.com", publisher="StateLife"
    )


@pytest.mark.parametrize(
    "values",
    [
        None,  # no file
        {"server_url": "http://dropshare.example.com", "publisher": "StateLife"},
        {"server_url": "https://dropshare.example.com", "publisher": ""},
        {"server_url": "https://dropshare.example.com"},
        {"server_url": "https://dropshare.example.com", "publisher": "StateLife", "x": 1},
    ],
)
def test_an_installed_build_without_valid_values_refuses_to_start(
    tmp_path: Path, values: dict[str, object] | None
) -> None:
    path = tmp_path / "build_info.json"
    if values is not None:
        _write(path, **values)

    with pytest.raises(BuildInfoError):
        load(path, frozen=True)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows checks signatures")
def test_updates_are_never_verified_off_windows() -> None:
    assert isinstance(platform.current().signatures, UnavailableVerifier)
