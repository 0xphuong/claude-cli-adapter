#!/usr/bin/env bash
# End-to-end smoke test for the containerised adapter.
#
#   ./smoke-test.sh                      # test the compose service on 127.0.0.1:8082
#   ./smoke-test.sh http://host:9000     # test somewhere else
#
# Stage A uses curl only (no dependencies). Stage B drives the real Anthropic
# SDK in a throwaway container, which is what actually matters — Hermes talks to
# the adapter through that SDK, not through curl.

set -uo pipefail

BASE="${1:-http://127.0.0.1:8082}"

# Test the model the service is actually configured to use. A gateway reached
# via ANTHROPIC_BASE_URL namespaces its models (e.g. "cc/claude-sonnet-5"), so
# the Anthropic default below would 404 there.
if [ -z "${ADAPTER_TEST_MODEL:-}" ] && [ -f .env ]; then
  eval "$(grep -E '^ADAPTER_DEFAULT_MODEL=' .env || true)"
fi
MODEL="${ADAPTER_TEST_MODEL:-${ADAPTER_DEFAULT_MODEL:-claude-sonnet-4-6}}"
COMPOSE_NET="${COMPOSE_NET:-claude-subscription-adapter_default}"
pass=0 fail=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n     %s\n' "$1" "${2:-}"; fail=$((fail + 1)); }

check() { # name, jq-free grep pattern, curl args...
  local name="$1" want="$2"; shift 2
  local out
  out=$(curl -s -m 240 "$@" 2>&1)
  if grep -q "$want" <<<"$out"; then ok "$name"; else bad "$name" "${out:0:300}"; fi
}

echo "Adapter smoke test -> $BASE (model $MODEL)"
echo
echo "Preflight"

if ! curl -sf -m 10 "$BASE/health" >/dev/null 2>&1; then
  bad "adapter reachable" "no response from $BASE/health — is it running? (docker compose up -d)"
  echo; echo "0 passed, 1 failed"; exit 1
fi
ok "adapter reachable"

auth=$(docker compose run --rm -T adapter claude auth status 2>/dev/null)
method=$(sed -n 's/.*"authMethod": *"\([^"]*\)".*/\1/p' <<<"$auth")
if grep -q '"loggedIn": *true' <<<"$auth"; then
  # claude.ai = stored subscription login; oauth_token = ANTHROPIC_AUTH_TOKEN,
  # which is also how a request is routed to a non-Anthropic gateway.
  ok "claude CLI authenticated (authMethod=${method:-unknown})"
else
  bad "claude CLI authenticated" \
      "log in with: docker compose run --rm adapter claude auth login
     or set ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN in .env"
  echo; echo "$pass passed, $fail failed"; exit 1
fi

echo
echo "Stage A — HTTP surface (curl)"

check "GET /health" '"status":"ok"' "$BASE/health"
check "GET /v1/models" '"data"' "$BASE/v1/models"

check "POST /v1/messages (non-streaming)" '"type":"text"' \
  -X POST "$BASE/v1/messages" -H 'content-type: application/json' -H 'x-api-key: dummy' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":64,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: PONG\"}]}"

check "POST /v1/messages (streaming SSE)" 'event: message_stop' \
  -N -X POST "$BASE/v1/messages" -H 'content-type: application/json' -H 'x-api-key: dummy' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":64,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"Say: STREAM_OK\"}]}"

check "POST /v1/messages (tool_use)" '"stop_reason":"tool_use"' \
  -X POST "$BASE/v1/messages" -H 'content-type: application/json' -H 'x-api-key: dummy' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":512,\"tools\":[{\"name\":\"get_weather\",\"description\":\"Get current weather for a city.\",\"input_schema\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}],\"messages\":[{\"role\":\"user\",\"content\":\"Weather in Hanoi? Use the tool.\"}]}"

echo
echo "Stage B — Anthropic SDK fidelity (throwaway container)"

if docker network inspect "$COMPOSE_NET" >/dev/null 2>&1; then
  if docker run --rm --network "$COMPOSE_NET" \
       -e ADAPTER_URL="http://adapter:8082" -e ADAPTER_TEST_MODEL="$MODEL" \
       -v "$(cd "$(dirname "$0")" && pwd)/smoke-test.py:/t/smoke-test.py:ro" \
       python:3.13-slim sh -c 'pip install -q anthropic && python /t/smoke-test.py'
  then ok "SDK checks"
  else bad "SDK checks" "see output above"
  fi
else
  echo "  SKIP  compose network '$COMPOSE_NET' not found"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
