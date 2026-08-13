# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An HTTP server that exposes the **Claude Code CLI** as an **Anthropic Messages API**
endpoint. Clients built on the Anthropic SDK point at it and reach whatever the CLI
is authenticated against — a subscription, or another Anthropic-compatible endpoint.
It exists so a client can consume a subscription; if your backend already speaks the
Messages API, the client should talk to it directly instead of through this.

## Commands

```bash
# Development — --build is load-bearing, see "compose pulls before it builds"
docker compose up -d --build
docker compose logs -f

# Authenticate (mode 1). Use `run --rm`, not `exec`: exec bypasses the entrypoint
docker compose run --rm adapter claude auth login
docker compose run --rm adapter claude auth status

# Test: 8 checks, ~2 min (every request spawns a `claude -p` process)
./smoke-test.sh
./smoke-test.sh http://host:9000

# Just the SDK stage, against any endpoint — there is no per-check granularity
ADAPTER_URL=http://127.0.0.1:8082 ADAPTER_TEST_MODEL=... ADAPTER_API_KEY=... \
  python smoke-test.py

# Publish (linux/amd64 only)
./release-image.sh <tag> [--latest] [--no-push]
```

There is no linter, formatter, or test framework configured. `requirements.txt` is
FastAPI + uvicorn; everything else the image needs is the Node-based CLI.

## Architecture

The whole program is one translation, in `server.py`:

```
POST /v1/messages
  → _extract_system_text   system: str | [blocks]        → str
  → _build_system_prompt   tool schemas                  → natural-language instructions
  → _messages_to_prompt    messages[]                    → flat "Human:/Assistant:" text
  → _run_claude            subprocess `claude -p ... --output-format json`
  → _parse_tool_calls      <tool_call>{...}</tool_call>   → tool_use blocks
  → _make_response / _sse_stream
```

Two consequences worth holding onto:

**Tool use is prompt engineering, not native tool use.** `_build_system_prompt`
appends instructions telling the model to emit `<tool_call>` tags; `_parse_tool_calls`
regex-extracts them back into `tool_use` blocks with freshly minted `toolu_` ids. Ids
therefore do not round-trip, and a model that ignores the format silently produces a
text-only response.

**Auth mode is decided entirely by environment, and both modes run the same
`claude -p`.** Mode 1 uses a stored subscription login (or `CLAUDE_CODE_OAUTH_TOKEN`);
mode 2 sets `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` to route the CLI elsewhere.
No code branches on this. `claude auth status` reports `claude.ai` vs `oauth_token`.

The image carries **two toolchains** — Node to run the CLI, Python to run the server.
All Dockerfile stages share one Debian bookworm base so the prebuilt venv's
interpreter symlinks stay valid in the runtime stage. The service runs as non-root
with `server.py` read-only to it, and `claude -p` inherits cwd `/workspace`, kept
empty so the CLI can neither reach the source nor pick up a stray `CLAUDE.md`.

## Traps

These each cost real debugging time. They are not obvious from any single file.

**An empty credential variable is worse than an unset one.** docker-compose injects
`""` for a bare `VAR=` line in `.env`, and the CLI reads an empty value as "use this
auth method", shadowing a stored login. `docker-entrypoint.sh` normalises every
credential variable back to unset — but `docker compose exec` bypasses the
entrypoint, so keep unused lines commented out rather than blank.

**The HOME volume must cover all of `/home/app`, not just `~/.claude`.** The CLI keeps
state in both `~/.claude/` and `~/.claude.json`. Persisting only the directory leaves
a half-written state, and the CLI then prints config warnings **to stdout** — where
`_run_claude`'s `json.loads` fails and the raw-text fallback returns the warnings as
if they were model output.

**The CLI reports failures on stdout, not stderr.** "Not logged in · Please run
/login" arrives as JSON on stdout with a non-zero exit and an empty stderr. This is
why `_run_claude` parses stdout first and treats the exit code as corroborating
rather than deciding — reversing that order returns an empty error message.

**compose pulls before it builds.** With both `image:` and `build:` set, `up` reuses a
matching local image, else **tries to pull**, and builds only if the pull fails. On
amd64 the pull succeeds, so a fresh checkout silently runs the *published* image
instead of local code. Always pass `--build` when testing changes. On arm64 the
amd64-only pull fails and it falls back to building, which makes it look like it
builds by default.

**`docker build` can leave nothing behind.** If the active buildx builder uses the
`docker-container` driver — likely, if any project on the machine created one — a
plain `docker build` puts no image in the local store; the build appears to succeed
and the image is nowhere. `release-image.sh` uses `docker buildx build` with an
explicit `--push` or `--load`.

**Model names must match the backend.** In mode 2 a gateway usually namespaces them
(`cc/claude-sonnet-5`). `ADAPTER_DEFAULT_MODEL` covers requests that send no `model`,
but `GET /v1/models` returns a **hardcoded Anthropic list** that this setting does not
touch — a client picking from it will ask for a model the backend does not have.

## Known gaps

Do not treat these as newly discovered bugs, and do not build on the behaviour they
imply:

- **No `/v1/messages/count_tokens`** — SDK clients calling it get a 404. `smoke-test.py`
  reports this as `SKIP`, not a failure.
- **The prompt is passed as a command-line argument**, so a long conversation hits
  `ARG_MAX` (`E2BIG`). Piping via stdin is the fix.
- **Streaming is emulated** — the CLI runs to completion, then the response is chunked
  at 32 characters. Time-to-first-token is the full request duration.
- **Request fields are dropped:** `max_tokens`, `tool_choice`, `stop_sequences`,
  `thinking`. `stop_reason` is never `max_tokens`.
- **`content: []` is reachable** when tools are requested and the model returns neither
  text nor a parseable tool call; the Anthropic SDK rejects that.
- **The adapter has no authentication.** It ignores the API key entirely — any value,
  or none, is accepted. Loopback binding in compose is the only control; publishing on
  `0.0.0.0` exposes the backing subscription to anyone who can reach the port.
- **One subprocess per request, no concurrency cap.** The compose resource limits are
  what bounds a burst.
- Published images are **`linux/amd64` only**, deliberately. `docker compose pull`
  failing on arm64 is expected; develop there with `--build`.

## Git

The remote must use the account's SSH host alias, not `github.com`, or pushes fall
back to the wrong key:

```
git@github-0xphuong:0xphuong/claude-cli-adapter.git
```

Other conventions (Conventional Commits, no `Co-Authored-By` trailer, SemVer tags)
come from the workspace-level `CLAUDE.md` one directory up.
