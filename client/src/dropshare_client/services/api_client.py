"""HTTP access to the DropShare server (architecture.md §4).

Every request carries X-Client-Version; responses are parsed into the shared envelope models.
On 401 the access token is refreshed once, under a lock, and the request is retried once.
Logs method, path, status, duration and request ID; never headers, tokens or bodies.
"""

import contextlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import NoReturn

import httpx
from pydantic import BaseModel, ValidationError

from dropshare_client.services.errors import (
    ApiError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_client.services.tokens import TokenStore
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.auth import RefreshIn, TokenPairOut
from dropshare_shared.schemas.envelope import DataResponse, ErrorResponse
from dropshare_shared.schemas.error_details import UpgradeRequiredDetails
from dropshare_shared.version import CLIENT_VERSION_HEADER

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
_REFRESH_PATH = "/api/v1/auth/refresh"
_INVALID_RESPONSE = "INVALID_RESPONSE"  # client-side code: the body wasn't what we expected
# Failures where the request never reached the server, so nothing was used up on the way.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Refresh answers given before the token is looked at: the client version check (426) runs
# before the endpoint, and validation (400) and proxy rate limiting (429) reject the request.
_REFUSED_BEFORE_USE = frozenset({400, 426, 429})
# Answers from a proxy in front of the server, which usually aren't in the error envelope.
_GATEWAY_ERRORS = frozenset({502, 503, 504})

type Json = dict[str, object]
type Params = dict[str, str | int]


@dataclass(frozen=True)
class _Call:
    """What a request carries besides its method and path."""

    json: Json | None = None
    params: Params | None = None
    content: bytes | None = None
    headers: dict[str, str] | None = None
    timeout: httpx.Timeout | None = None  # None: the client's default


class _ObjectData(BaseModel):
    """`{data: {...}}` before `data` is parsed as the endpoint's model."""

    data: dict[str, object]


class ApiClient:
    def __init__(
        self,
        base_url: str,
        tokens: TokenStore,
        *,
        client_version: str,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._tokens = tokens
        self._headers = {CLIENT_VERSION_HEADER: client_version, "User-Agent": user_agent}
        self._transport = transport
        self._http = self._new_http(base_url)

    def _new_http(self, base_url: str) -> httpx.Client:
        # TLS verification is always on (NFR-SEC-01); never make it configurable.
        return httpx.Client(
            base_url=base_url,
            headers=self._headers,
            timeout=_TIMEOUT,
            verify=True,
            transport=self._transport,
        )

    @property
    def base_url(self) -> str:
        return str(self._http.base_url).rstrip("/")

    def set_base_url(self, base_url: str) -> None:
        """Point at another server. Only while signed out: in-flight requests would break."""
        old, self._http = self._http, self._new_http(base_url)
        old.close()

    def close(self) -> None:
        self._http.close()

    def data[T: BaseModel](
        self,
        method: str,
        path: str,
        model: type[T],
        *,
        json: Json | None = None,
        params: Params | None = None,
        timeout: httpx.Timeout | None = None,
        authenticated: bool = True,
    ) -> T:
        """Send a request and return its `data`, parsed as `model`."""
        response = self._request(
            method, path, _Call(json=json, params=params, timeout=timeout), authenticated
        )
        try:
            return model.model_validate(_ObjectData.model_validate_json(response.content).data)
        except ValidationError:
            raise self._invalid_response(response) from None

    def envelope[T: BaseModel](
        self, method: str, path: str, model: type[T], *, params: Params | None = None
    ) -> T:
        """Send a request and parse the whole response body (`{data, meta}` lists)."""
        response = self._request(method, path, _Call(params=params), True)
        try:
            return model.model_validate_json(response.content)
        except ValidationError:
            raise self._invalid_response(response) from None

    def no_content(
        self,
        method: str,
        path: str,
        *,
        json: Json | None = None,
        params: Params | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> None:
        call = _Call(json=json, params=params, content=content, headers=headers)
        self._request(method, path, call, authenticated)

    @contextlib.contextmanager
    def stream(
        self, method: str, path: str, *, headers: dict[str, str] | None = None
    ) -> Iterator[httpx.Response]:
        """A response whose body is read by the caller (downloads). Errors are raised before
        it is handed over; a connection lost while reading raises httpx.TransportError."""
        call = _Call(headers=headers)
        token = self.ensure_access_token()
        response = self._send(method, path, call, token, stream=True)
        try:
            if response.status_code == 401:
                response.close()
                response = self._send(method, path, call, self._refresh(stale=token), stream=True)
            if not response.is_success:
                response.read()
                self._raise_error(response)
            yield response
        finally:
            response.close()

    def ensure_access_token(self) -> str:
        """An access token, refreshing first if there is none in memory (resume at startup)."""
        return self._tokens.access_token() or self._refresh(stale=None)

    def _request(self, method: str, path: str, call: _Call, authenticated: bool) -> httpx.Response:
        if not authenticated:
            response = self._send(method, path, call, token=None)
        else:
            token = self.ensure_access_token()
            response = self._send(method, path, call, token)
            if response.status_code == 401:
                response = self._send(method, path, call, self._refresh(stale=token))
        if response.is_success:
            return response
        self._raise_error(response)

    def _send(
        self,
        method: str,
        path: str,
        call: _Call,
        token: str | None,
        *,
        stream: bool = False,
        offline_on_failure: bool = True,
    ) -> httpx.Response:
        """One request, logged without headers or bodies. Transport failures become
        OfflineError, unless the caller needs to know exactly how the request failed."""
        headers = dict(call.headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = self._http.build_request(
            method,
            path,
            json=call.json,
            params=call.params,
            content=call.content,
            headers=headers,
            timeout=call.timeout if call.timeout is not None else httpx.USE_CLIENT_DEFAULT,
        )
        started = time.perf_counter()
        try:
            response = self._http.send(request, stream=stream)
        except httpx.TransportError as exc:
            logger.info("%s %s failed: %s", method, path, type(exc).__name__)
            if offline_on_failure:
                raise OfflineError("The server couldn't be reached") from exc
            raise
        logger.info(
            "%s %s -> %s in %.0f ms (request %s)",
            method,
            path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            response.headers.get("X-Request-ID", "-"),
        )
        return response

    def _refresh(self, stale: str | None) -> str:
        """A fresh access token. One refresh at a time; a thread that waited for another's
        refresh uses its result instead of refreshing again.

        A refresh token is single-use (OQ-16). It is taken out of the credential store before
        sending, and given back only when the request provably never reached the server or
        was refused before use (400, 426, 429). Any other outcome - no answer, 5xx from a
        proxy, an unreadable reply - may mean it was rotated, so the user signs in again.
        """
        with self._tokens.refresh_lock:
            current = self._tokens.access_token()
            if current is not None and current != stale:
                return current
            refresh_token = self._tokens.take_refresh_token()
            if refresh_token is None:
                raise SessionExpiredError()
            body = RefreshIn(refresh_token=refresh_token).model_dump()
            try:
                response = self._send(
                    "POST", _REFRESH_PATH, _Call(json=body), token=None, offline_on_failure=False
                )
            except _NOT_SENT as exc:
                self._tokens.give_back(refresh_token)
                raise OfflineError("The server couldn't be reached") from exc
            except httpx.TransportError as exc:
                logger.warning("Token refresh got no answer (%s); signing out", type(exc).__name__)
                self._expire()
                raise SessionExpiredError() from exc
            request_id = response.headers.get("X-Request-ID")
            if response.is_success:
                try:
                    pair = DataResponse[TokenPairOut].model_validate_json(response.content).data
                except ValidationError:
                    self._expire()
                    raise SessionExpiredError(request_id=request_id) from None
                self._tokens.store(pair)
                return pair.access_token
            if response.status_code in _REFUSED_BEFORE_USE:
                self._tokens.give_back(refresh_token)
                self._raise_error(response)
            self._expire()
            raise SessionExpiredError(self._error_code(response), request_id)

    def _expire(self) -> None:
        try:
            self._tokens.clear()
        except OSError:
            logger.exception("Couldn't remove the expired sign-in from the credential store")

    @staticmethod
    def _error_code(response: httpx.Response) -> ErrorCode | str | None:
        try:
            return ErrorResponse.model_validate_json(response.content).error.code
        except ValidationError:
            return None

    def _raise_error(self, response: httpx.Response) -> NoReturn:
        request_id = response.headers.get("X-Request-ID")
        try:
            error = ErrorResponse.model_validate_json(response.content).error
        except ValidationError:
            if response.status_code in _GATEWAY_ERRORS:
                raise OfflineError(f"The server is unavailable ({response.status_code})") from None
            if response.status_code == 429:
                raise ApiError(
                    429, ErrorCode.RATE_LIMITED, "Too many requests.", request_id=request_id
                ) from None
            raise self._invalid_response(response) from None
        if response.status_code == 426:
            min_version = None
            if isinstance(error.details, dict):
                with contextlib.suppress(ValidationError):
                    min_version = UpgradeRequiredDetails.model_validate(error.details).min_version
            raise UpgradeRequiredError(min_version)
        raise ApiError(response.status_code, error.code, error.message, error.details, request_id)

    @staticmethod
    def _invalid_response(response: httpx.Response) -> ApiError:
        return ApiError(
            response.status_code,
            _INVALID_RESPONSE,
            "The server sent a response this app doesn't understand.",
            request_id=response.headers.get("X-Request-ID"),
        )
