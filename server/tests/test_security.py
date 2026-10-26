import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from dropshare_server.core.security import (
    AccessClaims,
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_temporary_password,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)

SECRET = "unit-test-secret-0123456789abcdef0123"
CLAIMS = AccessClaims(
    user_id=uuid.uuid4(), device_id=uuid.uuid4(), token_version=3, must_change_password=True
)


def test_password_hash_is_argon2id_and_verifies() -> None:
    password_hash = hash_password("correct horse battery")

    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, "correct horse battery")
    assert not verify_password(password_hash, "wrong password")
    assert not verify_password("not-a-hash", "anything")


def test_temporary_password_meets_policy() -> None:
    assert len(generate_temporary_password()) >= 12
    assert generate_temporary_password() != generate_temporary_password()


def test_access_token_round_trip() -> None:
    token = create_access_token(CLAIMS, SECRET, datetime.now(UTC), timedelta(minutes=15))

    assert decode_access_token(token, SECRET) == CLAIMS


@pytest.mark.parametrize(
    "token",
    [
        create_access_token(
            CLAIMS, SECRET, datetime.now(UTC) - timedelta(hours=1), timedelta(minutes=15)
        ),
        create_access_token(
            CLAIMS, "another-secret-0123456789abcdef01", datetime.now(UTC), timedelta(minutes=15)
        ),
        jwt.encode({"sub": str(uuid.uuid4())}, SECRET, algorithm="HS256"),
        jwt.encode(
            {"sub": "x", "did": "y", "ver": 1, "pwc": False, "exp": 4102444800, "iat": 0}, SECRET
        ),
        "not-a-jwt",
    ],
    ids=["expired", "wrong-secret", "missing-claims", "bad-uuid", "garbage"],
)
def test_invalid_access_tokens_are_rejected(token: str) -> None:
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, SECRET)


def test_unsigned_token_is_rejected() -> None:
    unsigned = jwt.encode(
        {"sub": str(CLAIMS.user_id), "did": str(CLAIMS.device_id), "ver": 3, "pwc": False},
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(unsigned, SECRET)


def test_refresh_token_is_random_and_stored_only_as_hash() -> None:
    token, token_hash = new_refresh_token()

    assert len(token) >= 43  # 32 random bytes
    assert token not in token_hash
    assert hash_refresh_token(token) == token_hash
    assert new_refresh_token()[0] != token
