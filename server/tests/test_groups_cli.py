"""Group commands of the admin CLI (FR-ADMIN-01), audited as `cli` + OS user (FR-AUDIT-01)."""

from collections.abc import Callable

from dropshare_server.models import AuditEvent
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome

type Cli = Callable[..., tuple[int, str, str]]
type MakeUser = Callable[..., tuple[str, str]]
type AuditEvents = Callable[..., list[AuditEvent]]


def _ok(cli: Cli, *argv: str) -> str:
    code, out, err = cli(*argv)
    assert code == 0, err
    return out


def test_create_add_members_list_and_delete(
    cli: Cli, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    make_user("bob")
    make_user("carol")

    _ok(cli, "groups", "create", "  Finance  ")
    _ok(cli, "groups", "add-member", "finance", "bob")
    _ok(cli, "groups", "add-member", "FINANCE", "Carol")
    listed = _ok(cli, "groups", "list")
    _ok(cli, "groups", "remove-member", "Finance", "carol")
    after_removal = _ok(cli, "groups", "list")
    _ok(cli, "groups", "delete", "Finance")

    assert listed.splitlines()[1].split() == ["Finance", "2"]
    assert after_removal.splitlines()[1].split() == ["Finance", "1"]
    assert len(_ok(cli, "groups", "list").splitlines()) == 1  # only the header
    actions = [
        e.action
        for e in audit_events()
        if e.action.value.startswith("group.") and e.action != AuditAction.GROUP_LISTED
    ]
    assert actions == [
        AuditAction.GROUP_CREATED,
        AuditAction.GROUP_MEMBER_ADDED,
        AuditAction.GROUP_MEMBER_ADDED,
        AuditAction.GROUP_MEMBER_REMOVED,
        AuditAction.GROUP_DELETED,
    ]
    created = audit_events(AuditAction.GROUP_CREATED)[0]
    assert created.actor_type == AuditActorType.CLI
    assert created.actor_label == "it-admin"
    assert (created.target_type, created.target_name) == ("group", "Finance")


def test_group_names_are_unique_ignoring_case(cli: Cli, audit_events: AuditEvents) -> None:
    _ok(cli, "groups", "create", "Finance")

    code, _, err = cli("groups", "create", "FINANCE")

    assert code == 1 and "already exists" in err
    [failed] = [
        e for e in audit_events(AuditAction.GROUP_CREATED) if e.outcome == AuditOutcome.FAILURE
    ]
    assert (failed.target_type, failed.target_name) == ("group", "FINANCE")


def test_membership_errors_are_clear_and_audited(
    cli: Cli, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    make_user("bob")
    _ok(cli, "groups", "create", "Finance")
    _ok(cli, "groups", "add-member", "Finance", "bob")

    cases = [
        (("groups", "add-member", "Finance", "bob"), "already in"),
        (("groups", "add-member", "Finance", "nobody"), "No user named"),
        (("groups", "add-member", "Sales", "bob"), "No group named"),
        (("groups", "remove-member", "Finance", "nobody"), "No user named"),
        (("groups", "delete", "Sales"), "No group named"),
        (("groups", "create", "   "), "1-255 characters"),
    ]
    for argv, message in cases:
        code, _, err = cli(*argv)
        assert code == 1 and message in err, (argv, err)

    failures = [e for e in audit_events() if e.outcome == AuditOutcome.FAILURE]
    assert len(failures) == len(cases)


def test_removing_someone_not_in_the_group_says_so(cli: Cli, make_user: MakeUser) -> None:
    make_user("bob")
    _ok(cli, "groups", "create", "Finance")

    code, _, err = cli("groups", "remove-member", "Finance", "bob")

    assert code == 1 and "isn't in" in err
