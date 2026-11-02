"""NFR-SEC-04: new passwords on the bundled common-password list are refused (OQ-14)."""

from importlib.resources import files

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from dropshare_server.core.password_policy import is_common
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


def _change(api: TestClient, user: dict[str, str], new_password: str) -> Response:
    response: Response = api.post(
        "/api/v1/me/password",
        json={"current_password": user["password"], "new_password": new_password},
        headers={"Authorization": f"Bearer {user['access_token']}"},
    )
    return response


@pytest.mark.parametrize("password", ["passwordpassword", "PasswordPassword", "QWERTYUIOP12"])
def test_a_common_password_is_refused_on_the_field_and_nothing_changes(
    api: TestClient, signed_in: dict[str, str], password: str
) -> None:
    response = _change(api, signed_in, password)

    assert response.status_code == 400
    (issue,) = response.json()["error"]["details"]
    assert (issue["loc"], issue["type"]) == (["body", "new_password"], "password_common")
    assert password not in response.text
    # Still the old password: the refused change didn't sign anyone out either.
    assert _change(api, signed_in, "a-different-long-pass-77").status_code == 200


def test_an_uncommon_password_is_accepted(api: TestClient, signed_in: dict[str, str]) -> None:
    assert _change(api, signed_in, "violet tram ledger 1907").status_code == 200


def test_the_bundled_list_holds_only_what_the_length_rules_would_accept() -> None:
    text = files("dropshare_server.core").joinpath("common_passwords.txt").read_text("utf-8")
    entries = text.splitlines()

    assert len(entries) > 20_000
    assert all(PASSWORD_MIN_LENGTH <= len(entry) <= PASSWORD_MAX_LENGTH for entry in entries)
    assert all(normalize_name(entry) == entry for entry in entries)
    assert entries == sorted(set(entries))


def test_matching_ignores_case_but_nothing_else() -> None:
    assert is_common("iloveyou1234")
    assert is_common("ILoveYou1234")
    assert not is_common(" iloveyou1234")  # no trimming: that's a different password
    assert not is_common("iloveyou1234zq")
