#!/bin/sh
set -e

# An EMPTY credential variable is worse than an unset one: Claude Code sees the
# variable as present, selects that auth method, and never falls back to the
# next one — so a stored login is silently ignored. docker-compose injects ""
# whenever .env contains a bare `VAR=` line, so normalise those back to unset.
#
# Covers the server process and `docker compose run`. `docker compose exec`
# bypasses the entrypoint — keep unused lines commented out in .env, not blank.
for _v in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_BASE_URL; do
    eval "_val=\${$_v-}"
    [ -n "$_val" ] || unset "$_v"
done
unset _v _val

# Setting both makes the SDK send two auth headers, which the API rejects.
if [ -n "${ANTHROPIC_API_KEY-}" ] && [ -n "${ANTHROPIC_AUTH_TOKEN-}" ]; then
    echo "entrypoint: refusing to start — ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are both set." >&2
    echo "            Claude Code sends both headers and the endpoint rejects the request. Set one." >&2
    exit 1
fi

exec "$@"
