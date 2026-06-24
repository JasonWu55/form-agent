FROM python:3.12-slim

WORKDIR /app

# uv for fast, reproducible installs
RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY agent ./agent

RUN uv pip install --system --no-cache .

ENV AGENT_DB_PATH=/data/agent.db \
    SURVEY_PATH=/app/agent/survey.yaml

# Interactive TUI: run with `docker compose run --rm form-agent` (needs a TTY).
CMD ["python", "-m", "agent"]
