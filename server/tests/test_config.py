import pytest
from pydantic import ValidationError

from dropshare_server.core.config import DatabaseSettings, Settings

_REQUIRED = ("DB_HOST", "DB_SERVICE_NAME", "DB_USER", "DB_PASSWORD", "STORAGE_LOCAL_ROOT")


def test_missing_required_settings_refuse_to_load(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*_REQUIRED, "TOKEN_SECRET"):
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
        "token_secret",
    }


def test_short_token_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_SECRET", "too-short")

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert "token_secret" in {error["loc"][0] for error in caught.value.errors()}


@pytest.mark.parametrize("value", ["123456.0.0", "1.2.3-beta.1", "1.2", "latest"])
def test_min_client_version_must_be_a_version_the_check_can_compare(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # A value the request check couldn't parse would silently switch the check off (FR-UPD-04).
    monkeypatch.setenv("MIN_CLIENT_VERSION", value)

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert "min_client_version" in {error["loc"][0] for error in caught.value.errors()}


def test_database_settings_need_only_database_values(monkeypatch: pytest.MonkeyPatch) -> None:
    # Alembic uses these: it must not require the token secret or storage settings.
    for name in ("TOKEN_SECRET", "STORAGE_LOCAL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (("DB_HOST", "h"), ("DB_SERVICE_NAME", "s"), ("DB_USER", "u")):
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DB_PASSWORD", "p")

    assert DatabaseSettings(_env_file=None).db_host == "h"


def test_config_errors_never_echo_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "super-secret-db-password")
    monkeypatch.setenv("TOKEN_SECRET", "short-secret-token")

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    # str/repr are what tracebacks and startup logs show.
    for shown in (str(caught.value), repr(caught.value)):
        assert "super-secret-db-password" not in shown
        assert "short-secret-token" not in shown


def test_secrets_are_never_shown_in_repr() -> None:
    settings = Settings.model_validate(
        {
            "db_host": "h",
            "db_service_name": "s",
            "db_user": "u",
            "db_password": "super-secret",
            "storage_local_root": "storage",
            "token_secret": "token-secret-0123456789abcdef0123456789",
        }
    )

    assert "super-secret" not in repr(settings)
    assert "token-secret" not in repr(settings)
