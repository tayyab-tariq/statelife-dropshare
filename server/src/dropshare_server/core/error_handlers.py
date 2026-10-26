"""Central exception handlers: every error leaves the server in the standard envelope."""

from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from dropshare_server.core.errors import AppError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import ErrorBody, ErrorResponse

# Framework-raised HTTP errors (routing, security helpers, body parsing) mapped to stable codes.
_HTTP_ERRORS: dict[int, tuple[ErrorCode, str]] = {
    400: (ErrorCode.VALIDATION_ERROR, "The request is not valid"),
    401: (ErrorCode.UNAUTHENTICATED, "Sign in to continue"),
    403: (ErrorCode.FORBIDDEN, "You don't have permission to do that"),
    404: (ErrorCode.NOT_FOUND, "Not found"),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "Method not allowed"),
    413: (ErrorCode.FILE_TOO_LARGE, "The request is too large"),
    429: (ErrorCode.RATE_LIMITED, "Too many requests, try again later"),
    503: (ErrorCode.SERVICE_UNAVAILABLE, "The server isn't ready"),
}


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict[str, object] | list[object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this type only
    # Only location, message and type: the rejected input itself may contain secrets.
    details: list[object] = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return error_response(400, ErrorCode.VALIDATION_ERROR, "The request is not valid", details)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - registered for this type only
    if exc.status_code in _HTTP_ERRORS:
        code, message = _HTTP_ERRORS[exc.status_code]
        status_code = exc.status_code
    elif exc.status_code < 500:
        code, message = _HTTP_ERRORS[400]
        status_code = exc.status_code
    else:
        code, message, status_code = ErrorCode.INTERNAL_ERROR, "Something went wrong", 500
    return error_response(status_code, code, message, headers=exc.headers)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
