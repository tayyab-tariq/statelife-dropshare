"""Admin CLI (FR-ADMIN-01): the only way to administer v1. Calls services, never repositories,
and audits every action, successful or not, as actor `cli` plus the operating-system user."""

import argparse
import getpass
import math
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.db import create_db_engine, create_session_factory
from dropshare_server.core.errors import AppError
from dropshare_server.services import audit as audit_service
from dropshare_server.services import uploads as upload_service
from dropshare_server.services import users as user_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.context import CallContext, cli_context
from dropshare_server.storage.factory import create_storage_backend
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.users import UserRole

_GIB = 1024**3
_MAX_QUOTA_GB = 1024**2  # 1 PiB: far above any real quota, well inside a 64-bit byte count

type Handler = Callable[[argparse.Namespace, sessionmaker[Session], Settings, CallContext], int]


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _create_user(
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, ctx: CallContext
) -> int:
    quota = settings.default_quota_bytes if args.quota_gb is None else int(args.quota_gb * _GIB)
    with sessions.begin() as session:
        created = user_service.create_user(
            session,
            ctx,
            username=args.username,
            email=args.email,
            display_name=args.display_name,
            role=UserRole(args.role),
            quota_bytes=quota,
        )
    _out(f"Created user '{created.user.username}' ({created.user.role}).")
    _out(f"Temporary password (shown once): {created.temporary_password}")
    _out("The user must change it at first sign-in.")
    return 0


def _reset_password(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        password = user_service.reset_password(session, ctx, args.username)
    _out(f"Password reset for '{args.username}'; all their sessions were signed out.")
    _out(f"Temporary password (shown once): {password}")
    return 0


def _set_disabled(disabled: bool) -> Handler:
    def handler(
        args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
    ) -> int:
        with sessions.begin() as session:
            user_service.set_disabled(session, ctx, args.username, disabled)
        if disabled:
            _out(f"Disabled '{args.username}' and signed out all their sessions.")
        else:
            _out(f"Enabled '{args.username}'.")
        return 0

    return handler


def _list_users(
    _args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        users = user_service.list_users(session, ctx)
    _out(f"{'USERNAME':<24} {'EMAIL':<32} {'ROLE':<6} {'STATUS':<9} QUOTA_GB")
    for user, status in users:
        _out(
            f"{user.username:<24} {user.email:<32} {user.role:<6} {status.value:<9} "
            f"{user.quota_bytes / _GIB:g}"
        )
    return 0


def _export_audit(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    """Write to a private temp file and publish it only after the export (and its own audit
    event) committed, so a failure never leaves a partial file and nothing is overwritten."""
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(str(output))
    partial = output.with_name(f".{output.name}.{uuid.uuid4().hex}.partial")
    # 0o600: the export holds IPs and user names; other local users mustn't read it.
    descriptor = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with (
            os.fdopen(descriptor, "w", encoding="utf-8", newline="") as out,
            sessions.begin() as session,
        ):
            filters = audit_service.ExportFilters(
                occurred_from=_day_start(args.date_from),
                occurred_to=_day_start(args.date_to + timedelta(days=1)) if args.date_to else None,
                actor_user_id=user_service.find_user_id(session, args.user) if args.user else None,
                action=AuditAction(args.action) if args.action else None,
                outcome=AuditOutcome(args.outcome) if args.outcome else None,
            )
            rows = audit_service.export_csv(session, ctx, out, filters)
        os.link(partial, output)  # fails if the target appeared meanwhile: never overwrites
    finally:
        partial.unlink(missing_ok=True)
    _out(f"Exported {rows} audit events to {output}.")
    return 0


def _run_job(
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, _: CallContext
) -> int:
    """Scheduled maintenance, run by the OS scheduler (NFR-REL-05). Jobs log what they did."""
    match args.job:
        case "expire-uploads":
            result = upload_service.expire_uploads(
                sessions, create_storage_backend(settings), datetime.now(UTC)
            )
            _out(
                f"Expired {result.expired} upload(s); "
                f"removed {result.swept} staged upload folder(s)."
            )
    return 0


def _day_start(day: date | None) -> datetime | None:
    return None if day is None else datetime.combine(day, time.min, tzinfo=UTC)


def _quota_gb(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of GB") from None
    if not math.isfinite(value) or not 0 <= value <= _MAX_QUOTA_GB:
        raise argparse.ArgumentTypeError(f"must be between 0 and {_MAX_QUOTA_GB} GB")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dropshare-server", description="StateLife DropShare administration"
    )
    groups = parser.add_subparsers(dest="group", required=True)

    users = groups.add_parser("users", help="Manage user accounts").add_subparsers(
        dest="command", required=True
    )
    create = users.add_parser("create", help="Create a user with a one-time temporary password")
    create.add_argument("--username", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--role", choices=[role.value for role in UserRole], default="user")
    create.add_argument("--quota-gb", type=_quota_gb, help="Default: DEFAULT_QUOTA_BYTES (50 GB)")
    create.set_defaults(handler=_create_user, audit_action=AuditAction.USER_CREATED)
    for name, handler, action, help_text in (
        (
            "reset-password",
            _reset_password,
            AuditAction.AUTH_PASSWORD_RESET,
            "Set a new temporary password; signs out everywhere",
        ),
        (
            "disable",
            _set_disabled(True),
            AuditAction.USER_DISABLED,
            "Disable a user and sign out all their sessions",
        ),
        ("enable", _set_disabled(False), AuditAction.USER_ENABLED, "Enable a disabled user"),
    ):
        command = users.add_parser(name, help=help_text)
        command.add_argument("username")
        command.set_defaults(handler=handler, audit_action=action)
    users.add_parser("list", help="List all users").set_defaults(
        handler=_list_users, audit_action=AuditAction.USER_LISTED
    )

    audit = groups.add_parser("audit", help="Audit log").add_subparsers(
        dest="command", required=True
    )
    export = audit.add_parser("export", help="Export audit events to a new CSV file")
    export.add_argument("--output", required=True, help="CSV file to create (never overwritten)")
    export.add_argument("--from", dest="date_from", type=date.fromisoformat, help="YYYY-MM-DD")
    export.add_argument(
        "--to", dest="date_to", type=date.fromisoformat, help="YYYY-MM-DD, inclusive"
    )
    export.add_argument("--user", help="Only events by this username")
    export.add_argument("--action", choices=[action.value for action in AuditAction])
    export.add_argument("--outcome", choices=[outcome.value for outcome in AuditOutcome])
    export.set_defaults(handler=_export_audit, audit_action=AuditAction.AUDIT_EXPORTED)

    jobs = groups.add_parser("jobs", help="Scheduled maintenance jobs").add_subparsers(
        dest="command", required=True
    )
    run = jobs.add_parser("run", help="Run one job now")
    run.add_argument("job", choices=["expire-uploads"])
    run.set_defaults(handler=_run_job, audit_action=None)
    return parser


def _record_failure(
    sessions: sessionmaker[Session], ctx: CallContext, args: argparse.Namespace, code: str
) -> None:
    """Failed commands are audited too (FR-ADMIN-01), in their own transaction. Maintenance
    jobs aren't administrative actions and have no audit action."""
    if args.audit_action is None:
        return
    username: str | None = getattr(args, "username", None)
    audit_service.record_now(
        sessions,
        ctx,
        args.audit_action,
        AuditOutcome.FAILURE,
        AuditTarget(type="user", id=username, name=username) if username else None,
        {"command": f"{args.group} {args.command}", "code": code},
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as exc:
        fields = ", ".join(sorted({str(error["loc"][0]).upper() for error in exc.errors()}))
        _err(f"Configuration is missing or invalid ({fields}). See server/.env.example.")
        return 2

    engine = create_db_engine(settings)
    sessions = create_session_factory(engine)
    ctx = cli_context(getpass.getuser())
    try:
        handler: Handler = args.handler
        return handler(args, sessions, settings, ctx)
    except AppError as exc:
        _record_failure(sessions, ctx, args, exc.code.value)
        _err(exc.message)
        return 1
    except FileExistsError:
        _record_failure(sessions, ctx, args, "OUTPUT_EXISTS")
        _err(f"{args.output} already exists; choose another --output file.")
        return 1
    except IntegrityError as exc:
        if "UQ_USERS_" in str(exc.orig).upper():
            # Two admins creating the same user at once: the database's unique index caught it.
            _record_failure(sessions, ctx, args, "NAME_CONFLICT")
            _err("That username or email is already in use.")
            return 1
        return _database_error(exc)
    except SQLAlchemyError as exc:
        return _database_error(exc)
    finally:
        engine.dispose()


def _database_error(exc: SQLAlchemyError) -> int:
    _err(
        f"The database couldn't complete the command ({type(exc).__name__}). "
        "Check the DB_* settings and docs/oracle-setup.md."
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
