"""The one exception type services and routers raise; central handlers format it."""

from collections.abc import Mapping

from dropshare_shared.errors import ErrorCode


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, object] | list[object] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        audited: bool = False,
    ) -> None:
        """`headers` are added to the response (e.g. Retry-After). `audited=True` when the raiser
        already recorded the event (e.g. a failed sign-in), so the error handler doesn't add a
        second generic access-denied event."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = dict(headers) if headers else {}
        self.audited = audited
