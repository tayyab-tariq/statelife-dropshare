# DropShare API server image. Build from the repo root (the uv workspace):
#   docker build -f server/Dockerfile .
# The same image runs the API and the scheduled jobs (docker-compose.yml).

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first, so code changes don't reinstall them. uv needs every workspace member's
# pyproject.toml to read the lock file, the client's included; the client itself isn't installed.
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY server/pyproject.toml server/
COPY client/pyproject.toml client/
RUN uv sync --frozen --no-dev --package dropshare-server --no-install-workspace

COPY shared/ shared/
COPY server/ server/
RUN uv sync --frozen --no-dev --package dropshare-server --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin dropshare
COPY --from=build /app/.venv /app/.venv
COPY server/alembic.ini /app/alembic.ini
COPY server/migrations /app/migrations
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app
USER dropshare
EXPOSE 8000

# Migrate, then serve. --no-proxy-headers: TRUSTED_PROXIES is the only place a proxy is trusted
# (docs/deploy/reverse-proxy.md). Workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW) must stay below
# PostgreSQL's 100 connections (docs/perf-results.md).
CMD ["/bin/sh", "-c", "alembic -c /app/alembic.ini upgrade head && exec uvicorn dropshare_server.main:app --host 0.0.0.0 --port 8000 --workers \"${API_WORKERS:-4}\" --no-proxy-headers"]
