"""Structured JSON logs carrying the request ID (NFR-OPS-01)."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Fields callers may pass via `extra=`; anything else is dropped so payloads can't leak by accident.
_EXTRA_FIELDS = ("method", "path", "status", "duration_ms", "user_id")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for field in _EXTRA_FIELDS:
            if field in record.__dict__:
                entry[field] = record.__dict__[field]
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    # Replace only our own handler so repeated app startups (tests) don't duplicate output.
    root.handlers[:] = [
        *(h for h in root.handlers if not isinstance(h.formatter, _JsonFormatter)),
        handler,
    ]
    root.setLevel(level)
    # Uvicorn installs plain-text handlers that don't propagate; route its logs through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # The request middleware already logs one line per request, without query strings.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
