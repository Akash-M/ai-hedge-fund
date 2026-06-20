# Container for the live eToro trading bridge. Runs ONE cycle and exits — drive
# it with a scheduler (Render Cron, Railway Cron, Fly Machines, or plain cron).
#
# Build from the repo root:
#   docker build -f live/Dockerfile -t ai-hedge-fund-live .
# Run one dry-run cycle:
#   docker run --rm --env-file live/.env ai-hedge-fund-live
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false

WORKDIR /app

RUN pip install --no-cache-dir poetry

# Install dependencies first (better layer caching).
COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-interaction --no-ansi --no-root

# App code.
COPY src ./src
COPY live ./live

# Persisted state (mount a volume here in production).
ENV AIHF_STATE_DIR=/app/live_state
VOLUME ["/app/live_state"]

# Default: one dry-run cycle. Override AIHF_DRY_RUN / pass --live to trade.
ENTRYPOINT ["python", "-m", "live.run_cycle", "--print"]
