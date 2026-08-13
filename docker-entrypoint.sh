#!/bin/sh
set -e

# An EMPTY CLAUDE_CODE_OAUTH_TOKEN is worse than an unset one: Claude Code sees
# the variable as present, selects token auth, and never falls back to the
# credentials in ~/.claude — so an in-container `claude auth login` is silently
# ignored. docker-compose injects it as "" whenever .env contains a bare
# `CLAUDE_CODE_OAUTH_TOKEN=` line, so normalise it back to unset here.
#
# Covers the server process and `docker compose run`. `docker compose exec`
# bypasses the entrypoint — keep the line commented out in .env, not blank.
[ -n "${CLAUDE_CODE_OAUTH_TOKEN-}" ] || unset CLAUDE_CODE_OAUTH_TOKEN

exec "$@"
