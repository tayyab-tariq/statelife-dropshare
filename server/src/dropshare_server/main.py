"""App factory: settings, logging, resources, middleware, error handlers, routers."""

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from dropshare_server.api import health
from dropshare_server.core.config import Settings
from dropshare_server.core.db import create_db_engine, create_session_factory
from dropshare_server.core.error_handlers import error_response, register_error_handlers
from dropshare_server.core.logging import configure_logging, request_id_var
from dropshare_server.dependencies.resources import AppResources
from dropshare_server.storage.factory import create_storage_backend
from dropshare_shared.errors import ErrorCode

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings load at startup, so invalid config stops the server (NFR-OPS-03)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or Settings()
        configure_logging(resolved.log_level)
        engine = create_db_engine(resolved)
        app.state.resources = AppResources(
            settings=resolved,
            engine=engine,
            session_factory=create_session_factory(engine),
            storage=create_storage_backend(resolved),
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


app = create_app()
