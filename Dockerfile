# syntax=docker/dockerfile:1

# Pinned, and given its own stage rather than an inline `COPY --from=ghcr.io/...`:
# Dependabot's docker ecosystem reads FROM lines, so this is what makes the uv
# version something it can raise a pull request against. `latest` here would
# also mean an upstream release could change a build with no commit to explain
# it. See "Maintenance" in CLAUDE.md.
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

FROM python:3.14-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies resolve from the lockfile in their own layer, so application
# edits do not force a reinstall on every build.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# collectstatic needs *a* key but not the real one; the runtime value arrives
# through the environment.
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput

# Non-root, and owner of the data directory so SQLite can write its WAL
# sidecar files. SQLite needs write access to the directory, not just the db.
RUN useradd --system --create-home --uid 1001 ioref \
    && mkdir -p /app/data \
    && chown -R ioref:ioref /app/data
USER ioref

VOLUME ["/app/data"]
EXPOSE 8000

# Reads SCRIPT_NAME because gunicorn rejects any request path that does not
# start with it once the app is mounted under a prefix; a hardcoded
# /api/v1/health/ 500s in that deployment.
#
# Sends Host: <first entry of ALLOWED_HOSTS> rather than the connection's own
# 127.0.0.1:8000, because Django's ALLOWED_HOSTS check runs on the Host header
# regardless of where the connection actually came from. Any deployment that
# sets ALLOWED_HOSTS (every production one) otherwise gets a 400 on every
# single probe, forever, which urlopen raises as an uncaught exception and
# exits non-zero. Falls back to 127.0.0.1 when ALLOWED_HOSTS is unset, which
# only matters for a bare local `docker run` with DEBUG=True and nothing
# else configured; Django itself auto-allows that host in that specific
# case, independent of what this sends.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('SCRIPT_NAME',''); host=os.environ.get('ALLOWED_HOSTS','').split(',')[0].strip() or '127.0.0.1'; req=urllib.request.Request(f'http://127.0.0.1:8000{p}/api/v1/health/', headers={'Host': host}); sys.exit(0 if urllib.request.urlopen(req).status==200 else 1)"

# Two workers: enough that a slow request does not block the next one, few
# enough that SQLite's single-writer lock stays uncontended.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 60"]
