"""Client settings: server address rules (OQ-12, FR-CLIENT-05) and atomic storage."""

from pathlib import Path

import pytest

from dropshare_client.services.config import ClientConfig, ConfigStore, check_server_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://dropshare.example.com/", "https://dropshare.example.com"),
        ("https://dropshare.example.com:8443", "https://dropshare.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_accepted_server_addresses(url: str, expected: str) -> None:
    assert check_server_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://dropshare.example.com",  # plain HTTP off this computer (NFR-SEC-01)
        "http://10.0.0.5:8000",
        "ftp://dropshare.example.com",
        "dropshare.example.com",
        "https://user:pass@dropshare.example.com",
        "https://dropshare.example.com/?next=x",
        "",
    ],
)
def test_rejected_server_addresses(url: str) -> None:
    with pytest.raises(ValueError, match=r"\S"):
        check_server_url(url)


def test_settings_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    ConfigStore(path).update(server_url="https://dropshare.example.com", sync_root=tmp_path)

    loaded = ConfigStore(path).config

    assert loaded == ClientConfig(server_url="https://dropshare.example.com", sync_root=tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]  # no temp files left


def test_unreadable_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")

    assert ConfigStore(path).config == ClientConfig()


def test_invalid_change_is_refused_and_nothing_is_saved(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")

    with pytest.raises(ValueError, match="https"):
        store.update(server_url="http://dropshare.example.com")

    assert store.config == ClientConfig()
    assert not (tmp_path / "config.json").exists()
