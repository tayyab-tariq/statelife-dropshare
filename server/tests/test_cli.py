"""Admin CLI (FR-ADMIN-01): accounts and audit export, always audited as `cli` + OS user."""

import csv
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic_settings import SettingsConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.cli import main as cli_main
from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.models import AuditEvent, Namespace, User
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome
from dropshare_shared.schemas.nodes import NamespaceKind
from dropshare_shared.schemas.users import UserRole, UserStatus

type Cli = Callable[..., tuple[int, str, str]]
type Login = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


def _temporary_password(out: str) -> str:
    match = re.search(r"Temporary password \(shown once\): (\S+)", out)
    assert match, out
    return match.group(1)


def _create(cli: Cli, username: str = "bob", email: str = "bob@example.com") -> str:
    code, out, err = cli(
        "users", "create", "--username", username, "--email", email, "--display-name", "Bob B"
    )
    assert code == 0, err
    return _temporary_password(out)


def test_create_user_prints_a_one_time_password_and_sets_defaults(
    cli: Cli,
    login: Login,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    temporary = _create(cli)

    assert len(temporary) >= 12
    with session_factory() as session:
        user = session.scalars(select(User)).one()
        assert user.role is UserRole.USER
        assert user.status is UserStatus.ACTIVE
        assert user.quota_bytes == 50 * 1024**3
        assert user.must_change_password
        assert temporary not in user.password_hash
        namespace = session.scalars(select(Namespace)).one()
        assert (namespace.kind, namespace.owner_user_id) == (NamespaceKind.PERSONAL, user.id)
    assert login("bob", temporary).json()["data"]["must_change_password"] is True
    (event,) = audit_events(AuditAction.USER_CREATED)
    assert (event.actor_type, event.actor_label) == (AuditActorType.CLI, "it-admin")
    assert event.target_name == "bob"


def test_create_user_with_role_and_quota(cli: Cli, session_factory: sessionmaker[Session]) -> None:
    code, _, _ = cli(
        "users", "create", "--username", "root.admin", "--email", "r@example.com",
        "--display-name", "Root", "--role", "admin", "--quota-gb", "1.5",
    )  # fmt: skip

    assert code == 0
    with session_factory() as session:
        user = session.scalars(select(User)).one()
        assert (user.role, user.quota_bytes) == (UserRole.ADMIN, int(1.5 * 1024**3))


@pytest.mark.parametrize(
    ("username", "email", "message"),
    [
        ("BOB", "other@example.com", "Username 'BOB' is already taken."),
        ("bobby", "Bob@Example.COM", "Email 'Bob@Example.COM' is already in use."),
    ],
)
def test_usernames_and_emails_are_unique_case_insensitively(
    cli: Cli, username: str, email: str, message: str
) -> None:
    _create(cli)

    code, _, err = cli(
        "users", "create", "--username", username, "--email", email, "--display-name", "X"
    )

    assert code == 1
    assert message in err


@pytest.mark.parametrize(
    ("username", "email"),
    [("ab", "a@example.com"), ("has space", "a@example.com"), ("ok-name", "not-an-email")],
)
def test_invalid_usernames_and_emails_are_rejected(cli: Cli, username: str, email: str) -> None:
    code, _, err = cli(
        "users", "create", "--username", username, "--email", email, "--display-name", "X"
    )

    assert code == 1
    assert err.strip()


def test_reset_password_signs_out_and_clears_lockout(
    cli: Cli, login: Login, api: TestClient, audit_events: AuditEvents
) -> None:
    temporary = _create(cli)
    session = login("bob", temporary).json()["data"]
    for _ in range(5):
        login("bob", "wrong-password")

    code, out, _ = cli("users", "reset-password", "bob")

    assert code == 0
    new_temporary = _temporary_password(out)
    refreshed = api.post("/api/v1/auth/refresh", json={"refresh_token": session["refresh_token"]})
    assert refreshed.status_code == 401
    assert login("bob", temporary).status_code == 401
    assert login("bob", new_temporary).status_code == 200
    assert audit_events(AuditAction.AUTH_PASSWORD_RESET)[0].actor_type is AuditActorType.CLI


def test_disable_and_enable(cli: Cli, login: Login, audit_events: AuditEvents) -> None:
    temporary = _create(cli)

    assert cli("users", "disable", "bob")[0] == 0
    assert login("bob", temporary).status_code == 403
    assert cli("users", "enable", "bob")[0] == 0
    assert login("bob", temporary).status_code == 200
    assert len(audit_events(AuditAction.USER_DISABLED)) == 1
    assert len(audit_events(AuditAction.USER_ENABLED)) == 1


def test_unknown_user_is_reported(cli: Cli) -> None:
    code, _, err = cli("users", "disable", "ghost")

    assert code == 1
    assert "No user named 'ghost'" in err


def test_list_users(cli: Cli) -> None:
    _create(cli)
    _create(cli, "carol", "carol@example.com")

    code, out, _ = cli("users", "list")

    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("USERNAME")
    assert [line.split()[0] for line in lines[1:]] == ["bob", "carol"]


def test_refuses_to_run_without_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _WithoutEnvFile(Settings):
        model_config = SettingsConfigDict(**{**Settings.model_config, "env_file": None})

    monkeypatch.setattr("dropshare_server.cli.Settings", _WithoutEnvFile)
    monkeypatch.setenv("DB_PASSWORD", "secret-should-not-print")
    monkeypatch.setenv("TOKEN_SECRET", "short")

    code = cli_main(["users", "list"])

    err = capsys.readouterr().err
    assert code == 2
    assert "Configuration is missing or invalid" in err
    assert "TOKEN_SECRET" in err
    assert "secret-should-not-print" not in err


# --- Audit export --------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_audit_export_writes_csv_and_is_itself_audited(
    cli: Cli, login: Login, tmp_path: Path, audit_events: AuditEvents
) -> None:
    temporary = _create(cli)
    login("bob", "wrong-password")
    login("bob", temporary)
    output = tmp_path / "audit.csv"

    code, out, _ = cli("audit", "export", "--output", str(output))

    assert code == 0
    rows = _read_csv(output)
    assert [row["action"] for row in rows] == ["user.created", "auth.login", "auth.login"]
    assert [row["outcome"] for row in rows[1:]] == ["failure", "success"]
    assert "Exported 3 audit events" in out
    (export,) = audit_events(AuditAction.AUDIT_EXPORTED)
    assert export.actor_label == "it-admin"
    assert '"rows": 3' in (export.details or "")


def test_audit_export_filters(cli: Cli, login: Login, tmp_path: Path) -> None:
    temporary = _create(cli)
    _create(cli, "carol", "carol@example.com")
    login("bob", "wrong-password")
    login("bob", temporary)
    output = tmp_path / "failures.csv"

    code, _, _ = cli(
        "audit", "export", "--output", str(output), "--user", "bob",
        "--action", "auth.login", "--outcome", "failure", "--from", "2000-01-01",
    )  # fmt: skip

    assert code == 0
    rows = _read_csv(output)
    assert len(rows) == 1
    assert (rows[0]["action"], rows[0]["outcome"]) == ("auth.login", "failure")


def test_audit_export_date_range_excludes_other_days(cli: Cli, tmp_path: Path) -> None:
    _create(cli)
    output = tmp_path / "old.csv"

    cli("audit", "export", "--output", str(output), "--from", "2000-01-01", "--to", "2000-12-31")

    assert _read_csv(output) == []


def test_audit_export_never_overwrites(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "audit.csv"
    output.write_text("keep me", encoding="utf-8")

    code, _, err = cli("audit", "export", "--output", str(output))

    assert code == 1
    assert "already exists" in err
    assert output.read_text(encoding="utf-8") == "keep me"


def test_audit_export_neutralises_spreadsheet_formulas(
    cli: Cli, login: Login, tmp_path: Path
) -> None:
    login('=HYPERLINK("http://evil")', "whatever-password")
    output = tmp_path / "audit.csv"

    cli("audit", "export", "--output", str(output), "--outcome", "failure")

    (row,) = _read_csv(output)
    assert row["actor_label"].startswith("'=")
    assert row["actor_type"] == AuditActorType.ANONYMOUS.value
    assert row["outcome"] == AuditOutcome.FAILURE.value


# --- Audit completeness and robustness (M2 audit S4-S7) -----------------------------------


def test_reset_on_a_locked_account_records_the_unlock(
    cli: Cli, login: Login, audit_events: AuditEvents
) -> None:
    _create(cli)
    for _ in range(5):
        login("bob", "wrong-password")

    cli("users", "reset-password", "bob")

    (unlock,) = audit_events(AuditAction.AUTH_UNLOCKED)
    assert unlock.actor_type is AuditActorType.CLI
    assert '"reason": "password_reset"' in (unlock.details or "")


def test_listing_users_is_audited(cli: Cli, audit_events: AuditEvents) -> None:
    _create(cli)

    cli("users", "list")

    (event,) = audit_events(AuditAction.USER_LISTED)
    assert (event.actor_label, event.outcome) == ("it-admin", AuditOutcome.SUCCESS)


def test_failed_commands_are_audited(cli: Cli, audit_events: AuditEvents) -> None:
    cli("users", "create", "--username", "x", "--email", "x@example.com", "--display-name", "X")
    cli("users", "disable", "ghost")

    (create,) = audit_events(AuditAction.USER_CREATED)
    (disable,) = audit_events(AuditAction.USER_DISABLED)
    assert (create.outcome, disable.outcome) == (AuditOutcome.FAILURE, AuditOutcome.FAILURE)
    assert create.actor_label == "it-admin"
    assert '"code": "NOT_FOUND"' in (disable.details or "")
    assert disable.target_name == "ghost"


@pytest.mark.parametrize("quota", ["nan", "inf", "-1", "many"])
def test_quota_must_be_a_finite_non_negative_number(
    cli: Cli, quota: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli(
            "users", "create", "--username", "bob", "--email", "b@example.com",
            "--display-name", "Bob", "--quota-gb", quota,
        )  # fmt: skip

    assert caught.value.code == 2
    assert "--quota-gb" in capsys.readouterr().err


def test_unreachable_database_gives_a_clear_message(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "1")

    code, out, err = cli("users", "list")

    assert code == 3
    assert "The database couldn't complete the command" in err
    assert "Traceback" not in err + out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_audit_export_is_private_to_its_owner(cli: Cli, tmp_path: Path) -> None:
    output = tmp_path / "audit.csv"

    cli("audit", "export", "--output", str(output))

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_failed_export_leaves_no_file_behind(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create(cli)

    def fail_midway(session: Session, ctx: object, out: TextIO, filters: object) -> int:
        out.write("id,occurred_at\n1,partial\n")
        raise AppError(500, ErrorCode.INTERNAL_ERROR, "Export failed midway.")

    monkeypatch.setattr("dropshare_server.services.audit.export_csv", fail_midway)
    output = tmp_path / "audit.csv"

    code, _, err = cli("audit", "export", "--output", str(output))

    assert code == 1
    assert "Export failed midway." in err
    assert list(tmp_path.iterdir()) == [tmp_path / "storage"]


def test_expire_uploads_job_runs_from_the_cli(cli: Cli) -> None:
    code, out, _ = cli("jobs", "run", "expire-uploads")

    assert code == 0
    assert out == "Expired 0 upload(s); removed 0 staged upload folder(s).\n"
