# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.18 AS uv

FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_NO_CACHE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --home /app app
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY contracts ./contracts
COPY src ./src
RUN uv sync --frozen --no-dev
USER 10001:10001
EXPOSE 8000
CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "kosto_vet.bootstrap.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
