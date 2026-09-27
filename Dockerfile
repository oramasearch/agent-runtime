# syntax=docker/dockerfile:1
# agent-runtime (zetta#1016): the one image every user-created Python agent runs on. Private on
# Docker Hub as oramasearch/agent-runtime. The agent itself is loaded at start from Zetta or a lake
# repository, so no agent ever needs an image of its own.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home agent
WORKDIR /home/agent
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    TRACE_DIR=/tmp/agent-runtime-traces \
    PORT=8080
USER 10001
EXPOSE 8080
ENTRYPOINT ["agent-runtime"]
