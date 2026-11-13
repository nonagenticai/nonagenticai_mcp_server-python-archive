#!/usr/bin/env bash
set -euo pipefail
uv sync --frozen
printf '%s\n' 'Dependencies installed. Set DATABASE_URL explicitly, run uv run alembic upgrade head, then uv run python -m src.run_mcp.'
