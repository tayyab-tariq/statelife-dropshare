"""App factory: settings, logging, resources, middleware, error handlers, routers."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from dropshare_server.api import health
from dropshare_server.api import v1 as api_v1
from dropshare_server.core.config import Settings
from dropshare_server.core.db import create_db_engine, create_session_factory
from dropshare_server.core.error_handlers import error_response, register_error_handlers
from dropshare_server.core.logging import configure_logging, request_id_var
from dropshare_server.dependencies.context import is_https
from dropshare_server.dependencies.resources import AppResources
from dropshare_server.scanner.factory import create_scanner
from dropshare_server.services.file_policy import FileTypePolicy
from dropshare_server.services.releases import MinimumVersion
from dropshare_server.storage.factory import create_storage_backend
from dropshare_shared.errors import ErrorCode

logger = logging.getLogger(__name__)

# NFR-SEC-01. The reverse proxy sends the same header (docs/deploy/reverse-proxy.md); this one
# is the fallback. No includeSubDomains: other hosts on the organization's domain aren't ours.
_HSTS = "max-age=31536000"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings load at startup, so invalid config stops the server (NFR-OPS-03)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or Settings()
        configure_logging(resolved.log_level)
        engine = create_db_engine(resolved)
        session_factory = create_session_factory(engine)
        app.state.resources = AppResources(
            settings=resolved,
            engine=engine,
            session_factory=session_factory,
            storage=create_storage_backend(resolved),
            minimum_version=MinimumVersion(resolved.min_client_version, session_factory),
            file_types=FileTypePolicy(frozenset(resolved.banned_extensions), session_factory),
            scanner=create_scanner(resolved, reply_timeout=resolved.scan_inline_timeout_seconds),
            # /health/ready needs no sign-in: it must never hold a request for long.
            scanner_probe=create_scanner(
                resolved, reply_timeout=resolved.clamd_connect_timeout_seconds
            ),
        )
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="StateLife DropShare",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_error_handlers(app)
    app.middleware("http")(_request_context)
    app.include_router(health.router)
    app.include_router(api_v1.router)
    return app


async def _request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Assign a request ID, log one line per request, and turn unexpected errors into a 500."""
    request_id = uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        try:
            response = await call_next(request)
        except Exception:  # last-resort boundary: log, then answer with a generic 500
            logger.exception(
                "Unhandled error", extra={"method": request.method, "path": request.url.path}
            )
            response = error_response(500, ErrorCode.INTERNAL_ERROR, "Something went wrong")
        response.headers["X-Request-ID"] = request_id
        if _reached_over_https(request):
            response.headers["Strict-Transport-Security"] = _HSTS
        logger.info(
            "Request handled",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response
    finally:
        request_id_var.reset(token)


def _reached_over_https(request: Request) -> bool:
    """HSTS only over HTTPS: browsers ignore it on plain HTTP, and a development server on
    http://localhost must never pin itself (NFR-SEC-01, OQ-12). Runs outside the last-resort
    boundary, so it must not raise: without resources (startup failed) no proxy is trusted."""
    resources = getattr(request.app.state, "resources", None)
    return is_https(
        request.url.scheme,
        request.client.host if request.client else None,
        request.headers.get("X-Forwarded-Proto"),
        resources.settings.trusted_proxies if isinstance(resources, AppResources) else [],
    )


app = create_app()
