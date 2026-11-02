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
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import groups as group_service
from dropshare_server.services import releases as release_service
from dropshare_server.services import storage_report as storage_report_service
from dropshare_server.services import trash as trash_service
from dropshare_server.services import uploads as upload_service
from dropshare_server.services import users as user_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.context import CallContext, cli_context
from dropshare_server.services.releases import NewRelease
from dropshare_server.storage.factory import create_storage_backend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.releases import ReleaseChannel, ReleasePlatform
from dropshare_shared.schemas.users import UserRole
from dropshare_shared.signing import system_verifier

_GIB = 1024**3
_MIB = 1024**2
_MAX_QUOTA_GB = 1024**2  # 1 PiB: far above any real quota, well inside a 64-bit byte count

type Handler = Callable[[argparse.Namespace, sessionmaker[Session], Settings, CallContext], int]


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _create_user(
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, ctx: CallContext
) -> int:
    quota = settings.default_quota_bytes if args.quota_bytes is None else args.quota_bytes
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


def _set_quota(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        user = user_service.set_quota(session, ctx, args.username, args.quota_bytes)
    _out(f"Quota for '{user.username}' set to {user.quota_bytes / _GIB:g} GB.")
    return 0


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


def _set_channel(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        user = user_service.set_update_channel(
            session, ctx, args.username, ReleaseChannel(args.channel)
        )
    _out(f"'{user.username}' is now offered {args.channel} releases.")
    return 0


def _publish_release(
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, ctx: CallContext
) -> int:
    """Checks the installer's signature here (FR-UPD-02), so it runs on Windows (OQ-48)."""
    try:
        notes = args.notes_file.read_text(encoding="utf-8").strip() if args.notes_file else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise AppError(
            400, ErrorCode.VALIDATION_ERROR, f"Couldn't read the notes as UTF-8 text: {exc}"
        ) from exc
    release = NewRelease(
        platform=ReleasePlatform(args.platform),
        channel=ReleaseChannel(args.channel),
        version=args.release_version,
        min_supported_version=args.min_supported,
        notes=notes,
        sha256=args.sha256,
    )
    try:
        with sessions.begin() as session:
            published = release_service.publish(
                session,
                create_storage_backend(settings),
                system_verifier(),
                settings.release_signer(),
                ctx,
                release,
                args.installer,
            )
    except OSError as exc:
        raise AppError(
            500,
            ErrorCode.INTERNAL_ERROR,
            f"Couldn't read the installer or store it ({type(exc).__name__}: {exc.strerror}); "
            "nothing was published.",
        ) from exc
    _out(
        f"Published {args.platform} {published.channel} {published.version} "
        f"({published.size_bytes / _MIB:.1f} MB, minimum supported "
        f"{published.min_supported_version})."
    )
    return 0


def _list_releases(
    _args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, __: CallContext
) -> int:
    with sessions() as session:
        releases = release_service.list_releases(session)
    _out(
        f"{'PLATFORM':<12} {'CHANNEL':<8} {'VERSION':<10} {'MIN':<10} {'SIZE_MB':>8}  "
        f"{'PUBLISHED':<16} STATUS"
    )
    for release in releases:
        status = "withdrawn" if release.withdrawn_at else "available"
        _out(
            f"{release.platform.value:<12} {release.channel.value:<8} {release.version:<10} "
            f"{release.min_supported_version:<10} {release.size_bytes / _MIB:>8.1f}  "
            f"{release.published_at:%Y-%m-%d %H:%M} {status}"
        )
    return 0


def _withdraw_release(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        release_service.withdraw(
            session,
            ctx,
            ReleasePlatform(args.platform),
            ReleaseChannel(args.channel),
            args.release_version,
        )
    _out(
        f"Withdrew {args.platform} {args.channel} {args.release_version}; "
        "clients are no longer offered it."
    )
    return 0


def _storage_report(
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, ctx: CallContext
) -> int:
    try:
        stored_blobs = storage_report_service.count_blobs(create_storage_backend(settings))
    except OSError as exc:
        # Unmounted or unreadable: no traceback or path (it shows where files are stored), but
        # audited like any failed command.
        _record_failure(sessions, ctx, args, "STORAGE_UNAVAILABLE")
        _err(f"The file storage couldn't be read ({type(exc).__name__}). Check STORAGE_* settings.")
        return 1
    with sessions.begin() as session:
        report = storage_report_service.build(session, ctx, stored_blobs)
    _out(f"{'USERNAME':<24} {'STATUS':<9} {'QUOTA_GB':>10} {'USED_GB':>10} {'USED%':>6} TRASH_GB")
    for user in report.users:
        _out(
            f"{user.username:<24} {user.status.value:<9} {user.quota_bytes / _GIB:>10.2f} "
            f"{user.used_bytes / _GIB:>10.2f} {_percent(user.used_bytes, user.quota_bytes):>6} "
            f"{user.trash_bytes / _GIB:.2f}"
        )
    _out("")
    _out(f"Users: {len(report.users)}")
    _out(f"Used: {report.used_bytes / _GIB:.2f} GB of {report.quota_bytes / _GIB:.2f} GB allocated")
    _out(
        f"File versions: {report.version_count} ({report.version_bytes / _GIB:.2f} GB); "
        f"blobs in storage: {report.stored_blobs}"
    )
    return 0


def _percent(used: int, quota: int) -> str:
    if quota == 0:
        return "-" if used == 0 else "full"
    return f"{used * 100 / quota:.0f}%"


def _create_group(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        group_service.create_group(session, ctx, args.group_name)
    _out(f"Created group '{args.group_name.strip()}'.")
    return 0


def _delete_group(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        group_service.delete_group(session, ctx, args.group_name)
    _out(f"Deleted group '{args.group_name}'; access shared with it was removed.")
    return 0


def _add_group_member(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        group_service.add_member(session, ctx, args.group_name, args.username)
    _out(f"Added '{args.username}' to '{args.group_name}'.")
    return 0


def _remove_group_member(
    args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        group_service.remove_member(session, ctx, args.group_name, args.username)
    _out(
        f"Removed '{args.username}' from '{args.group_name}'; access through the group ends "
        "at their next sync."
    )
    return 0


def _list_groups(
    _args: argparse.Namespace, sessions: sessionmaker[Session], _: Settings, ctx: CallContext
) -> int:
    with sessions.begin() as session:
        groups = group_service.list_groups(session, ctx)
    _out(f"{'GROUP':<40} MEMBERS")
    for group in groups:
        _out(f"{group.name:<40} {group.member_count}")
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
    args: argparse.Namespace, sessions: sessionmaker[Session], settings: Settings, ctx: CallContext
) -> int:
    """Scheduled maintenance, run by the OS scheduler (NFR-REL-05). Jobs log what they did."""
    now = datetime.now(UTC)
    match args.job:
        case "expire-uploads":
            result = upload_service.expire_uploads(sessions, create_storage_backend(settings), now)
            _out(
                f"Expired {result.expired} upload(s); "
                f"removed {result.swept} staged upload folder(s)."
            )
        case "purge-trash":
            purged = trash_service.purge_expired(
                sessions, create_storage_backend(settings), ctx, now, settings.trash_retention_days
            )
            _out(
                f"Permanently deleted {purged.items} item(s) from the trash, "
                f"freeing {purged.freed_bytes} bytes."
            )
        case "gc-blobs":
            try:
                collected = blob_service.collect(
                    sessions,
                    create_storage_backend(settings),
                    now,
                    settings.blob_grace_hours,
                    force=args.force,
                )
            except blob_service.TooManyOrphansError as exc:
                _err(
                    f"Stopped: {exc.orphans} of {exc.blobs} stored files have no record in this "
                    "database, which looks like the wrong database or an older backup. Nothing "
                    "unreferenced was deleted. Check the DB_* settings; if this is expected, "
                    "run again with --force."
                )
                return 1
            _out(
                f"Removed {collected.versions} replaced version(s) and "
                f"{collected.orphans} unreferenced blob(s)."
            )
        case "purge-audit":
            removed = _purge_audit(sessions, settings, ctx, now)
            _out(
                f"Removed {removed} audit event(s) older than {settings.audit_retention_days} days."
            )
    return 0


def _purge_audit(
    sessions: sessionmaker[Session], settings: Settings, ctx: CallContext, now: datetime
) -> int:
    """Through the purge account when one is set (FR-AUDIT-03), else the app's own."""
    if settings.audit_purge_db_user is None:
        _err(
            "AUDIT_PURGE_DB_USER isn't set: purging with the app's own account. In production, "
            "only a separate account should be able to delete audit events."
        )
        return audit_service.purge_expired(
            sessions, sessions, ctx, now, settings.audit_retention_days
        )
    engine = create_db_engine(settings.audit_purge_database())
    try:
        return audit_service.purge_expired(
            create_session_factory(engine), sessions, ctx, now, settings.audit_retention_days
        )
    finally:
        engine.dispose()


def _day_start(day: date | None) -> datetime | None:
    return None if day is None else datetime.combine(day, time.min, tzinfo=UTC)


def _quota_bytes(text: str) -> int:
    """--quota-gb: a number of GB (decimals allowed), as bytes."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of GB") from None
    if not math.isfinite(value) or not 0 <= value <= _MAX_QUOTA_GB:
        raise argparse.ArgumentTypeError(f"must be between 0 and {_MAX_QUOTA_GB} GB")
    return int(value * _GIB)


def _existing_file(text: str) -> Path:
    path = Path(text)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"no such file: {text}")
    return path


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
    create.add_argument(
        "--quota-gb",
        dest="quota_bytes",
        type=_quota_bytes,
        help="Default: DEFAULT_QUOTA_BYTES (50 GB)",
    )
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
    set_quota = users.add_parser(
        "set-quota", help="Change a user's quota; never below what they already use"
    )
    set_quota.add_argument("username")
    set_quota.add_argument("--quota-gb", dest="quota_bytes", type=_quota_bytes, required=True)
    set_quota.set_defaults(handler=_set_quota, audit_action=AuditAction.USER_QUOTA_CHANGED)
    set_channel = users.add_parser(
        "set-channel", help="Choose which client releases a user is offered (default stable)"
    )
    set_channel.add_argument("username")
    set_channel.add_argument("channel", choices=[channel.value for channel in ReleaseChannel])
    set_channel.set_defaults(handler=_set_channel, audit_action=AuditAction.USER_CHANNEL_CHANGED)
    users.add_parser("list", help="List all users").set_defaults(
        handler=_list_users, audit_action=AuditAction.USER_LISTED
    )

    group_commands = groups.add_parser("groups", help="Manage groups for sharing").add_subparsers(
        dest="command", required=True
    )
    for name, handler, action, help_text in (
        ("create", _create_group, AuditAction.GROUP_CREATED, "Create a group"),
        (
            "delete",
            _delete_group,
            AuditAction.GROUP_DELETED,
            "Delete a group; access shared with it ends",
        ),
    ):
        command = group_commands.add_parser(name, help=help_text)
        command.add_argument("group_name", metavar="GROUP")
        command.set_defaults(handler=handler, audit_action=action)
    for name, handler, action, help_text in (
        ("add-member", _add_group_member, AuditAction.GROUP_MEMBER_ADDED, "Add a user to a group"),
        (
            "remove-member",
            _remove_group_member,
            AuditAction.GROUP_MEMBER_REMOVED,
            "Remove a user from a group",
        ),
    ):
        command = group_commands.add_parser(name, help=help_text)
        command.add_argument("group_name", metavar="GROUP")
        command.add_argument("username")
        command.set_defaults(handler=handler, audit_action=action)
    group_commands.add_parser("list", help="List groups and their member counts").set_defaults(
        handler=_list_groups, audit_action=AuditAction.GROUP_LISTED
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

    storage = groups.add_parser("storage", help="Storage usage").add_subparsers(
        dest="command", required=True
    )
    storage.add_parser(
        "report", help="Quota and usage per user, and totals of what is stored"
    ).set_defaults(handler=_storage_report, audit_action=AuditAction.STORAGE_REPORTED)

    releases = groups.add_parser("releases", help="Desktop client releases").add_subparsers(
        dest="command", required=True
    )
    channels = [channel.value for channel in ReleaseChannel]
    platforms = [platform.value for platform in ReleasePlatform]
    publish = releases.add_parser(
        "publish", help="Publish a signed installer; checks its signature (Windows only)"
    )
    publish.add_argument(
        "--version", dest="release_version", metavar="VERSION", required=True, help="1.2.3"
    )
    publish.add_argument("--channel", choices=channels, required=True)
    publish.add_argument(
        "--min-supported",
        required=True,
        help="Older clients must update before they can sync (FR-UPD-04)",
    )
    publish.add_argument("--installer", type=_existing_file, required=True)
    publish.add_argument("--sha256", required=True, help="The installer's SHA-256, in hex")
    publish.add_argument("--notes-file", type=_existing_file, help="Release notes, UTF-8 text")
    publish.add_argument("--platform", choices=platforms, default=ReleasePlatform.WINDOWS_X64)
    publish.set_defaults(handler=_publish_release, audit_action=AuditAction.RELEASE_PUBLISHED)
    releases.add_parser("list", help="List releases, newest first").set_defaults(
        handler=_list_releases, audit_action=None
    )
    withdraw = releases.add_parser("withdraw", help="Stop offering a release to clients")
    withdraw.add_argument("release_version", metavar="VERSION")
    withdraw.add_argument("--channel", choices=channels, required=True)
    withdraw.add_argument("--platform", choices=platforms, default=ReleasePlatform.WINDOWS_X64)
    withdraw.set_defaults(handler=_withdraw_release, audit_action=AuditAction.RELEASE_WITHDRAWN)

    jobs = groups.add_parser("jobs", help="Scheduled maintenance jobs").add_subparsers(
        dest="command", required=True
    )
    run = jobs.add_parser("run", help="Run one job now")
    run.add_argument("job", choices=["expire-uploads", "purge-trash", "gc-blobs", "purge-audit"])
    run.add_argument(
        "--force",
        action="store_true",
        help="gc-blobs: delete unreferenced blobs even when unusually many look unreferenced",
    )
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
    group_name: str | None = getattr(args, "group_name", None)
    release_version: str | None = getattr(args, "release_version", None)
    target = None
    if release_version:
        name = f"{args.platform} {args.channel} {release_version}"
        target = AuditTarget(type="release", id=name, name=name)
    elif group_name:
        target = AuditTarget(type="group", id=group_name, name=group_name)
    elif username:
        target = AuditTarget(type="user", id=username, name=username)
    audit_service.record_now(
        sessions,
        ctx,
        args.audit_action,
        AuditOutcome.FAILURE,
        target,
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
        if "UQ_CLIENT_RELEASES_" in str(exc.orig).upper():
            _record_failure(sessions, ctx, args, "VERSION_CONFLICT")
            _err("That release was just published by someone else.")
            return 1
        if "UQ_GROUPS_" in str(exc.orig).upper() or "PK_GROUP_MEMBERS" in str(exc.orig).upper():
            _record_failure(sessions, ctx, args, "NAME_CONFLICT")
            _err("That group or membership was just created by someone else.")
            return 1
        return _database_error(exc)
    except SQLAlchemyError as exc:
        return _database_error(exc)
    finally:
        engine.dispose()


def _database_error(exc: SQLAlchemyError) -> int:
    _err(
        f"The database couldn't complete the command ({type(exc).__name__}). "
        "Check the DB_* settings and docs/oracle-setup.md (or docs/postgres-setup.md)."
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
