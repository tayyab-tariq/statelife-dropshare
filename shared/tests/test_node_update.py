"""The `PATCH /nodes/{id}` body (FR-FILE-04, FR-SYNC-10)."""

import uuid

import pytest
from pydantic import ValidationError

from dropshare_shared.schemas.nodes import NodeUpdateIn


def test_a_rename_a_move_or_both_are_accepted() -> None:
    parent = uuid.uuid4()

    assert NodeUpdateIn.model_validate({"if_revision": 1, "name": "b"}).parent_id is None
    assert NodeUpdateIn.model_validate({"if_revision": 1, "parent_id": str(parent)}).name is None
    both = NodeUpdateIn.model_validate({"if_revision": 2, "name": "b", "parent_id": str(parent)})
    assert (both.name, both.parent_id) == ("b", parent)


@pytest.mark.parametrize(
    "body",
    [
        {"if_revision": 1},  # changes nothing
        {"name": "b"},  # no base revision
        {"if_revision": 0, "name": "b"},
        {"if_revision": 1, "name": ""},
        {"if_revision": 1, "name": "b", "content_hash": "x"},  # extra="forbid"
    ],
)
def test_bodies_that_change_nothing_or_carry_extras_are_refused(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NodeUpdateIn.model_validate(body)
