"""Client settings: server address rules (OQ-12, FR-CLIENT-05) and atomic storage."""

import json
from pathlib import Path

import pytest

from dropshare_client.services.config import (
    DEFAULT_MAX_TRANSFERS,
    DEFAULT_SERVER_URL,
    MAX_TRANSFERS_LIMIT,
    ClientConfig,
    ConfigStore,
    NotificationSettings,
    check_server_url,
)
from dropshare_client.sync.core import MAX_TRANSFERS
from dropshare_client.sync.core import MAX_TRANSFERS_LIMIT as ENGINE_LIMIT


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


def test_the_built_in_server_address_is_not_saved(tmp_path: Path) -> None:
    """A later build's address applies; an address the user chose stays (FR-CLIENT-05)."""
    path = tmp_path / "config.json"
    ConfigStore(path).update(server_url=DEFAULT_SERVER_URL, sync_root=tmp_path)

    assert "server_url" not in json.loads(path.read_text(encoding="utf-8"))
    assert ConfigStore(path).config.server_url == DEFAULT_SERVER_URL


def test_once_an_account_signed_in_the_address_is_kept(tmp_path: Path) -> None:
    """So a later build with another built-in address doesn't move this client off the server
    its sync folder belongs to (audit 2026-11-02-0544 S5)."""
    path = tmp_path / "config.json"
    ConfigStore(path).update(server_url=DEFAULT_SERVER_URL, keep_server_url=True)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert (saved["server_url"], saved["keep_server_url"]) == (DEFAULT_SERVER_URL, True)


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


def test_settings_from_an_older_version_get_the_new_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"server_url": "https://dropshare.example.com", "sync_root": "/x"}')

    config = ConfigStore(path).config

    assert config.sync_root == Path("/x")
    assert config.notifications == NotificationSettings()  # all on (FR-CLIENT-02)
    assert config.autostart is None  # not decided yet: turned on at the first sign-in
    assert config.max_transfers == DEFAULT_MAX_TRANSFERS


@pytest.mark.parametrize(("saved", "loaded"), [(0, 1), (3, 3), (99, MAX_TRANSFERS_LIMIT)])
def test_transfers_at_once_out_of_range_is_clamped_not_lost(
    tmp_path: Path, saved: int, loaded: int
) -> None:
    path = tmp_path / "config.json"
    path.write_text(f'{{"sync_root": "/x", "max_transfers": {saved}}}')

    config = ConfigStore(path).config

    assert (config.max_transfers, config.sync_root) == (loaded, Path("/x"))


def test_notification_and_startup_choices_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    ConfigStore(path).update(
        notifications=NotificationSettings(conflicts=False), autostart=False, max_transfers=2
    )

    loaded = ConfigStore(path).config

    assert loaded.notifications == NotificationSettings(conflicts=False)
    assert (loaded.autostart, loaded.max_transfers) == (False, 2)


def test_the_settings_range_matches_what_the_engine_allows() -> None:
    assert (DEFAULT_MAX_TRANSFERS, MAX_TRANSFERS_LIMIT) == (MAX_TRANSFERS, ENGINE_LIMIT)
