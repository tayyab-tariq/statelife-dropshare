"""Sign-in, resume, password change and sign-out (FR-AUTH-01, 02, 04, 05)."""

import logging
import platform as os_platform
import socket
from dataclasses import dataclass

from pydantic import ValidationError

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import (
    ApiError,
    InvalidInputError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_client.services.tokens import TokenStore
from dropshare_shared.schemas.auth import LoginIn, PasswordChangeIn, TokenPairOut
from dropshare_shared.schemas.users import UserOut

logger = logging.getLogger(__name__)

_DEVICE_NAME_MAX = 255
_DEVICE_OS_MAX = 64


@dataclass(frozen=True)
class Device:
    """How this install appears in the user's device list (FR-AUTH-07)."""

    name: str
    os: str


def this_device() -> Device:
    name = socket.gethostname().strip() or "This computer"
    system = {"Darwin": "macOS"}.get(os_platform.system(), os_platform.system())
    return Device(
        name=name[:_DEVICE_NAME_MAX],
        os=f"{system} {os_platform.release()}".strip()[:_DEVICE_OS_MAX],
    )


@dataclass(frozen=True)
class Session:
    """Who is signed in. `user` is None until a temporary password has been replaced: the
    server refuses everything else until then (FR-AUTH-05)."""

    user: UserOut | None
    must_change_password: bool


@dataclass(frozen=True)
class SignOut:
    server_confirmed: bool  # False: the session ends on the server when its token expires
    forgotten_locally: bool  # False: the saved sign-in couldn't be removed from the store


class AuthService:
    """Blocking calls: run them off the UI thread (controllers do)."""

    def __init__(self, api: ApiClient, tokens: TokenStore, device: Device) -> None:
        self._api = api
        self._tokens = tokens
        self._device = device

    def sign_in(self, server_url: str, login: str, password: str) -> Session:
        if server_url != self._api.base_url:
            self._api.set_base_url(server_url)
        try:
            body = LoginIn(
                login=login,
                password=password,
                device_name=self._device.name,
                device_os=self._device.os,
            )
        except ValidationError:
            raise InvalidInputError("Check your username or email and password.") from None
        pair = self._api.data(
            "POST", "/api/v1/auth/login", TokenPairOut, json=body.model_dump(), authenticated=False
        )
        self._tokens.store(pair)
        return self._session()

    def resume(self) -> Session | None:
        """The saved session, or None when there is none (FR-AUTH-02). Raises OfflineError when
        the server can't be reached; the saved sign-in is kept for a later try."""
        if self._tokens.refresh_token() is None:
            return None
        self._api.ensure_access_token()
        return self._session()

    def _session(self) -> Session:
        if self._tokens.must_change_password:
            return Session(user=None, must_change_password=True)
        return Session(user=self.current_user(), must_change_password=False)

    def current_user(self) -> UserOut:
        return self._api.data("GET", "/api/v1/me", UserOut)

    def change_password(self, current_password: str, new_password: str) -> None:
        """Other sessions are signed out by the server; this one gets new tokens. Done once
        they're stored: loading the user afterwards is a separate step that may fail alone."""
        try:
            body = PasswordChangeIn(current_password=current_password, new_password=new_password)
        except ValidationError:
            raise InvalidInputError("Check the length of the passwords you entered.") from None
        pair = self._api.data("POST", "/api/v1/me/password", TokenPairOut, json=body.model_dump())
        self._tokens.store(pair)

    def sign_out(self) -> SignOut:
        """End the session on the server, then forget it here, whatever happened (FR-AUTH-04)."""
        server_confirmed = True
        try:
            self._api.no_content("POST", "/api/v1/auth/logout")
        except SessionExpiredError:
            # The refresh needed to sign out failed: the session may still be valid there.
            server_confirmed = False
        except ApiError as exc:
            server_confirmed = exc.status == 401  # 401: already over on the server
            if not server_confirmed:
                logger.warning("Sign-out wasn't confirmed by the server: %s", exc.code)
        except (OfflineError, UpgradeRequiredError) as exc:
            logger.warning("Signed out on this computer only: %s", type(exc).__name__)
            server_confirmed = False
        try:
            self._tokens.clear()
        except OSError:
            logger.exception("Couldn't remove the saved sign-in")
            return SignOut(server_confirmed=server_confirmed, forgotten_locally=False)
        return SignOut(server_confirmed=server_confirmed, forgotten_locally=True)
