# syntax=docker/dockerfile:1.7
#
# Claude CLI Adapter
# --------------------------------
# The runtime needs two toolchains: Node (to run the Claude Code CLI) and
# Python (to run the FastAPI adapter). Both build stages and the runtime share
# a single Debian bookworm base so the venv's interpreter symlinks stay valid.

ARG NODE_IMAGE=node:22-bookworm-slim
ARG CLAUDE_CODE_VERSION=2.1.229

# ---------------------------------------------------------------------------
# Stage 1 — Python dependencies (built into a self-contained venv)
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS python-deps

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copied on its own so a code change does not invalidate the dependency layer.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — Claude Code CLI (pinned; installed under its own prefix)
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS claude-cli

ARG CLAUDE_CODE_VERSION
RUN npm install -g --prefix /opt/claude \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force

# ---------------------------------------------------------------------------
# Stage 3 — Runtime
# ---------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS runtime

# python3 only (no venv/pip/build tooling) — the venv arrives prebuilt.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app app

COPY --from=python-deps /opt/venv /opt/venv
COPY --from=claude-cli  /opt/claude /opt/claude
RUN ln -s /opt/claude/bin/claude /usr/local/bin/claude

# Application code stays root-owned and read-only to the service account.
WORKDIR /app
COPY --chmod=0444 server.py /app/server.py
COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# `claude -p` inherits the server's cwd. Give it an empty, writable directory
# instead of /app so it can never touch the source, and so CLAUDE.md
# auto-discovery finds nothing.
RUN install -d -o app -g app -m 0755 /workspace /home/app/.claude

ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISABLE_AUTOUPDATER=1

USER app
WORKDIR /workspace

EXPOSE 8082

# server.py defaults to 127.0.0.1, which is unreachable from outside the
# container — bind 0.0.0.0 here and restrict exposure at the port mapping.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", \
       "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/opt/venv/bin/python", "/app/server.py", "--host", "0.0.0.0", "--port", "8082"]
