"""The one exception type services and routers raise; central handlers format it."""

from collections.abc import Mapping, Sequence

from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import ValidationIssue


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
        denied: bool = False,
        target: tuple[str, str] | None = None,
    ) -> None:
        """`headers` are added to the response (e.g. Retry-After). `audited=True` when the raiser
        already recorded the event (e.g. a failed sign-in), so the error handler doesn't add a
        second generic access-denied event. `denied=True` audits a non-401/403 error as access
        denied: a 404 that hides another user's item is still an authorization failure.
        `target` is the (type, id) that was refused, recorded in that audit event (FR-AUDIT-02)."""
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.headers = dict(headers) if headers else {}
        self.audited = audited
        self.denied = denied
        self.target = target


def invalid(message: str, issue_type: str, loc: Sequence[str | int] = ()) -> AppError:
    """400 VALIDATION_ERROR whose details are always a list of ValidationIssue, with a stable
    `type` the client can branch on."""
    issue = ValidationIssue(loc=list(loc), msg=message, type=issue_type)
    return AppError(400, ErrorCode.VALIDATION_ERROR, message, [issue.model_dump()])
