ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv

FROM base AS builder
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY runner ./runner
COPY toolkits ./toolkits
RUN uv sync --frozen --no-dev
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY scripts/reset_development_database.py scripts/grant_runtime_role.py scripts/configure_execution_capacity.py scripts/verify_execution_cluster.py scripts/acceptance_fixtures.py scripts/acceptance_worker.py scripts/run_cluster_acceptance.py ./scripts/

FROM builder AS worker-builder
RUN uv sync --frozen --no-dev --extra worker

FROM base AS worker
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app
COPY --from=worker-builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
CMD ["python", "-m", "src.execution.worker"]

FROM base AS production
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER 1000:1000
EXPOSE 8033
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8033/health/live', timeout=3)"]
CMD ["uvicorn", "src.asgi:app", "--host", "0.0.0.0", "--port", "8033"]
