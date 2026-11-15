# syntax=docker/dockerfile:1
# agent-runtime (zetta#1016): the one image every user-created Python agent runs on. Private on
# Docker Hub as oramasearch/agent-runtime. The agent itself is loaded at start from Zetta or a lake
# repository, so no agent ever needs an image of its own.
# Bases are pinned by their multi-platform index digest (lake#1003); a new base is a reviewed
# change of the digest, not a moved tag.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --uid 10001 --create-home agent
WORKDIR /home/agent
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    TRACE_DIR=/tmp/agent-runtime-traces \
    PORT=8080
USER 10001
EXPOSE 8080
ENTRYPOINT ["agent-runtime"]
