"""Sign-in, resume, password change and sign-out against the fake server (FR-AUTH-01/02/04/05)."""

from collections.abc import Callable

import pytest
from client_fakes import NEW_PASSWORD, SERVER_URL, USER_PASSWORD, Client

from dropshare_client.services.errors import ApiError, InvalidInputError
from dropshare_shared.errors import ErrorCode

URL = SERVER_URL

type MakeClient = Callable[[], Client]


def test_sign_in_saves_the_refresh_token_only_in_the_credential_store(
    make_client: MakeClient,
) -> None:
    client = make_client()

    session = client.auth.sign_in(URL, "alice", USER_PASSWORD)

    assert session.user is not None and session.user.username == "alice"
    assert client.credentials.secret in client.server.refresh
    saved = [path.read_text() for path in client.app_data.rglob("*") if path.is_file()]
    assert all(client.credentials.secret not in text for text in saved)


def test_wrong_password_is_an_unauthenticated_error(make_client: MakeClient) -> None:
    with pytest.raises(ApiError) as caught:
        make_client().auth.sign_in(URL, "alice", "wrong-password")

    assert caught.value.code == ErrorCode.UNAUTHENTICATED


def test_temporary_password_signs_in_without_a_user_until_changed(make_client: MakeClient) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True

    session = client.auth.sign_in(URL, "alice", USER_PASSWORD)
    client.auth.change_password(USER_PASSWORD, NEW_PASSWORD)

    assert (session.user, session.must_change_password) == (None, True)
    assert client.auth.current_user().username == "alice"
    assert client.credentials.secret in client.server.refresh


def test_relaunch_resumes_without_the_password(make_client: MakeClient) -> None:
    make_client().auth.sign_in(URL, "alice", USER_PASSWORD)

    relaunched = make_client()  # a new process: nothing in memory, token in the store
    session = relaunched.auth.resume()

    assert session is not None and session.user is not None
    assert session.user.username == "alice"


def test_resume_without_a_saved_session_returns_none(make_client: MakeClient) -> None:
    assert make_client().auth.resume() is None


def test_sign_out_revokes_on_the_server_and_forgets_locally(make_client: MakeClient) -> None:
    client = make_client()
    client.auth.sign_in(URL, "alice", USER_PASSWORD)

    result = client.auth.sign_out()

    assert (result.server_confirmed, result.forgotten_locally) == (True, True)
    assert client.credentials.secret is None
    assert client.server.refresh == {}


def test_sign_out_while_offline_still_forgets_locally(make_client: MakeClient) -> None:
    client = make_client()
    client.auth.sign_in(URL, "alice", USER_PASSWORD)
    client.server.offline = True

    result = client.auth.sign_out()

    assert (result.server_confirmed, result.forgotten_locally) == (False, True)
    assert client.credentials.secret is None


def test_sign_in_by_email_in_any_case(make_client: MakeClient) -> None:
    session = make_client().auth.sign_in(URL, "ALICE@Example.com", USER_PASSWORD)

    assert session.user is not None and session.user.username == "alice"


def test_too_long_input_is_refused_without_echoing_it(make_client: MakeClient) -> None:
    secret = "S3cretPassw0rd-" * 10  # over the 128-character limit

    with pytest.raises(InvalidInputError) as caught:
        make_client().auth.sign_in(URL, "alice", secret)

    assert "S3cret" not in str(caught.value)
    assert "S3cret" not in repr(caught.value.__cause__)


def test_sign_out_when_the_client_is_too_old_still_forgets_locally(
    make_client: MakeClient,
) -> None:
    client = make_client()
    client.auth.sign_in(URL, "alice", USER_PASSWORD)
    client.server.min_version = (1, 0, 0)  # this client (0.1.0) is now too old: 426

    result = client.auth.sign_out()

    assert (result.server_confirmed, result.forgotten_locally) == (False, True)
    assert client.credentials.secret is None
