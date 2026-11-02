"""Uninstall's "remove local app data" (FR-UPD-01, OQ-49): only DropShare's own files go."""

from pathlib import Path

from client_fakes import MemoryCredentials

from dropshare_client.services.app_data import remove_app_data


def _fill(app_data: Path) -> None:
    for name in ("config.json", "index.db", "index.db-wal", "instance.lock", "folder-move.json"):
        (app_data / name).parent.mkdir(parents=True, exist_ok=True)
        (app_data / name).write_text("x")
    (app_data / "logs").mkdir()
    (app_data / "logs" / "dropshare.log").write_text("x")
    (app_data / "logs" / "dropshare.log.1").write_text("x")
    (app_data / "updates").mkdir()
    (app_data / "updates" / "DropShare-1.2.0-setup.exe").write_text("x")
    (app_data / "updates" / "pending.json").write_text("{}")


def test_everything_of_ours_goes_with_the_saved_sign_in(tmp_path: Path) -> None:
    app_data = tmp_path / "StateLife" / "DropShare"
    _fill(app_data)
    credentials = MemoryCredentials()
    credentials.set("refresh-token")

    remove_app_data(app_data, credentials)

    assert credentials.get() is None
    assert not (tmp_path / "StateLife").exists()  # empty once ours is gone


def test_anything_else_stays_with_its_folders(tmp_path: Path) -> None:
    app_data = tmp_path / "StateLife" / "DropShare"
    _fill(app_data)
    (app_data / "logs" / "notes.txt").write_text("the user's")
    synced = app_data / "My sync folder"  # chosen inside app data, against advice
    synced.mkdir()
    (synced / "report.docx").write_text("the user's")
    (tmp_path / "StateLife" / "Other app").mkdir()

    remove_app_data(app_data, MemoryCredentials())

    assert (app_data / "logs" / "notes.txt").exists()
    assert (synced / "report.docx").exists()
    assert not (app_data / "config.json").exists()
    assert not (app_data / "updates").exists()
    assert (tmp_path / "StateLife" / "Other app").exists()
