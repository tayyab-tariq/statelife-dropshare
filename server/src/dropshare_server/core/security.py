"""Password hashing, access tokens and refresh tokens (NFR-SEC-03, FR-AUTH-03)."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id with the library's current recommended parameters
_JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so response time doesn't reveal unknown users."""
    verify_password(_dummy_hash(), password)


def generate_temporary_password() -> str:
    return secrets.token_urlsafe(15)  # 20 characters, above the 12-character minimum


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    device_id: uuid.UUID
    token_version: int
    must_change_password: bool


class InvalidAccessTokenError(Exception):
    pass


def create_access_token(
    claims: AccessClaims, secret: str, now: datetime, lifetime: timedelta
) -> str:
    payload = {
        "sub": str(claims.user_id),
        "did": str(claims.device_id),
        "ver": claims.token_version,
        "pwc": claims.must_change_password,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["sub", "did", "ver", "pwc", "exp", "iat"]},
        )
        return AccessClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            device_id=uuid.UUID(str(payload["did"])),
            token_version=int(payload["ver"]),
            must_change_password=bool(payload["pwc"]),
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise InvalidAccessTokenError from exc


def new_refresh_token() -> tuple[str, str]:
    """Return (token for the client, hash for the database). 256 bits of randomness."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    # A fast hash is right here: the token is random and high-entropy, unlike a password.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
