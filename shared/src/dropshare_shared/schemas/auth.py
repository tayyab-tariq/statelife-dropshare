"""Sign-in, token refresh and password change (FR-AUTH-01..05, NFR-SEC-04)."""

from pydantic import BaseModel, ConfigDict, Field

# NFR-SEC-04: 12-128 characters, no composition rules. Login accepts any length up to the max
# so existing passwords set under older rules still work.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


class LoginIn(BaseModel):
    # hide_input_in_errors: a validation error must never echo a password or token.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    login: str = Field(min_length=1, max_length=320, description="Username or email")
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    device_name: str = Field(min_length=1, max_length=255)
    device_os: str = Field(min_length=1, max_length=64)


class RefreshIn(BaseModel):
    # hide_input_in_errors: a validation error must never echo a password or token.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    refresh_token: str = Field(min_length=1, max_length=128)


class PasswordChangeIn(BaseModel):
    # hide_input_in_errors: a validation error must never echo a password or token.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    must_change_password: bool
