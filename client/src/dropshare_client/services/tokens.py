"""The signed-in session's tokens (FR-AUTH-02, NFR-SEC-03 client side).

The access token lives only in memory. The refresh token is kept in the OS credential store so
a restart resumes without the password. A refresh token is single-use (OQ-16): it is removed
from the store *before* it is sent, so a crash or a failed save afterwards can never make a
later launch send a used token again; the worst case is being asked for the password.
"""

import logging
import threading

from dropshare_client.platform.base import CredentialStore
from dropshare_shared.schemas.auth import TokenPairOut

logger = logging.getLogger(__name__)


class TokenStore:
    def __init__(self, credentials: CredentialStore) -> None:
        self._credentials = credentials
        # Held for the whole refresh, so concurrent requests trigger exactly one refresh.
        self.refresh_lock = threading.Lock()
        self._access: str | None = None
        self._refresh: str | None = None
        self._loaded = False
        self.must_change_password = False

    def access_token(self) -> str | None:
        return self._access

    def refresh_token(self) -> str | None:
        """The current refresh token; reads the credential store (blocking) the first time."""
        if not self._loaded:
            self._refresh = self._credentials.get()
            self._loaded = True
        return self._refresh

    def take_refresh_token(self) -> str | None:
        """Hand the refresh token over for sending: it is forgotten here and in the store.
        Call `give_back` if it provably never reached the server."""
        token = self.refresh_token()
        self._refresh = None
        if token is not None:
            self._forget_stored()
        return token

    def give_back(self, token: str) -> None:
        """The refresh request never left this computer, so the token is still unused."""
        self._refresh = token
        self._save(token)

    def store(self, pair: TokenPairOut) -> None:
        self._access = pair.access_token
        self._refresh = pair.refresh_token
        self._loaded = True
        self.must_change_password = pair.must_change_password
        self._save(pair.refresh_token)

    def clear(self) -> None:
        """Forget the session here and in the credential store. Raises OSError if the stored
        copy couldn't be removed, so sign-out can tell the user."""
        self._access = None
        self._refresh = None
        self._loaded = True
        self.must_change_password = False
        self._credentials.delete()

    def _save(self, token: str) -> None:
        try:
            self._credentials.set(token)
        except OSError:
            # The session still works; the next launch asks for the password. Remove whatever
            # is stored, which may be an older, already-used token.
            logger.exception("Couldn't save the sign-in; you'll need to sign in after a restart")
            self._forget_stored()

    def _forget_stored(self) -> None:
        try:
            self._credentials.delete()
        except OSError:
            logger.exception("Couldn't remove the saved sign-in from the credential store")
