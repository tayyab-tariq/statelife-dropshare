import pytest
from pydantic import ValidationError

from dropshare_server.core.config import Settings


def test_missing_required_settings_refuse_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DB_HOST", "DB_SERVICE_NAME", "DB_USER", "DB_PASSWORD", "STORAGE_LOCAL_ROOT"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    missing = {error["loc"][0] for error in caught.value.errors()}
    assert missing == {
        "db_host",
        "db_service_name",
        "db_user",
        "db_password",
        "storage_local_root",
    }


def test_password_is_never_shown_in_repr() -> None:
    settings = Settings.model_validate(
        {
            "db_host": "h",
            "db_service_name": "s",
            "db_user": "u",
            "db_password": "super-secret",
            "storage_local_root": "storage",
        }
    )

    assert "super-secret" not in repr(settings)
