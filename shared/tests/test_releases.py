import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dropshare_shared.schemas.releases import LatestReleaseQuery, ReleaseChannel, ReleaseOut
from dropshare_shared.schemas.users import UserOut


def _release(**overrides: object) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "version": "1.2.0",
        "channel": "stable",
        "min_supported_version": "1.0.0",
        "notes": "",
        "size_bytes": 1,
        "sha256": "0" * 64,
        "published_at": datetime.now(UTC).isoformat(),
        **overrides,
    }


def test_the_latest_query_takes_only_a_known_platform() -> None:
    assert LatestReleaseQuery.model_validate({"platform": "windows-x64"})
    for bad in ({"platform": "linux"}, {"platform": "windows-x64", "channel": "beta"}):
        with pytest.raises(ValidationError):
            LatestReleaseQuery.model_validate(bad)


@pytest.mark.parametrize("field", ["version", "min_supported_version"])
def test_release_versions_have_no_pre_release_suffix(field: str) -> None:
    with pytest.raises(ValidationError):
        ReleaseOut.model_validate(_release(**{field: "1.2.0-beta.1"}))


def test_a_channel_from_a_newer_server_still_parses() -> None:
    assert ReleaseOut.model_validate(_release(channel="nightly")).channel == "nightly"
    assert ReleaseOut.model_validate(_release(channel="beta")).channel is ReleaseChannel.BETA


def test_a_user_from_an_older_server_is_on_stable() -> None:
    user = UserOut.model_validate(
        {
            "id": str(uuid.uuid4()),
            "username": "alice",
            "email": "alice@example.com",
            "display_name": "Alice",
            "role": "user",
            "quota_bytes": 1,
        }
    )

    assert user.update_channel is ReleaseChannel.STABLE
