import uuid

import pytest
from pydantic import ValidationError

from dropshare_shared.schemas.sharing import MemberIn, SearchQuery, SharedItemOut, ShareRole


def test_a_member_is_either_a_user_or_a_group() -> None:
    user, group = uuid.uuid4(), uuid.uuid4()

    assert MemberIn(user_id=user, role=ShareRole.VIEWER).group_id is None
    assert MemberIn(group_id=group, role=ShareRole.EDITOR).user_id is None
    for both_or_neither in ({"user_id": user, "group_id": group}, {}):
        with pytest.raises(ValidationError):
            MemberIn.model_validate({**both_or_neither, "role": "viewer"})


def test_owner_is_never_a_role_that_can_be_given() -> None:
    with pytest.raises(ValidationError):
        MemberIn.model_validate({"user_id": str(uuid.uuid4()), "role": "owner"})


@pytest.mark.parametrize("q", ["", "a", "x" * 101])
def test_search_needs_two_to_a_hundred_characters(q: str) -> None:
    with pytest.raises(ValidationError):
        SearchQuery(q=q)


def test_a_role_from_a_newer_server_still_parses() -> None:
    item = SharedItemOut.model_validate(
        {
            "node_id": str(uuid.uuid4()),
            "namespace_id": None,
            "kind": "file",
            "name": "plan.txt",
            "owner": {"id": str(uuid.uuid4()), "display_name": "Ann", "email": "ann@example.com"},
            "role": "commenter",
            "shared_at": "2026-10-28T08:00:00Z",
            "size_bytes": 4,
            "modified_at": "2026-10-28T08:00:00Z",
            "mount_node_id": None,
        }
    )
    assert item.role == "commenter"
