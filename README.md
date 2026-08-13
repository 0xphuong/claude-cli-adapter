## Prerequisites

* Python 3.10+
* Claude Code CLI installed and authenticated (`claude` is on your `$PATH`,
  `claude -p "hi"` returns a response)
* Hermes Agent 0.14+

---

## Setup

```bash
# 1. Clone / download this adapter
git clone https://github.com/<your-fork>/claudehermessubscriptionadapter
cd claudehermessubscriptionadapter

# 2. Install dependencies (use a venv if you like)
pip install -r requirements.txt

# 3. Start the adapter
python server.py            # listens on 127.0.0.1:8082 by default
# or choose a different port:
python server.py --port 9000
```

Leave the adapter running in a terminal (or add it to a systemd/launchd unit).

---

## Docker

The image bundles both toolchains the adapter needs: Node (to run the Claude
Code CLI) and Python (to run the adapter itself).

### Run

```bash
docker compose up -d --build
curl http://127.0.0.1:8082/health          # {"status":"ok"}
```

The service starts fine before you log in — it just returns `502` on every
`/v1/messages` until you do.

### Authentication

The Claude Code CLI runs **inside** the container, and authenticates itself. It
never touches your host's login session, so do the login once in the container:

```bash
docker compose run --rm adapter claude auth login
```

It prints a sign-in URL (the container has no browser). Open it on your host,
approve, and paste the code back at the `Paste code here` prompt. Verify:

```bash
docker compose run --rm adapter claude auth status     # "loggedIn": true
```

> Use `run --rm`, not `exec`, for these two commands. `run` goes through the
> image entrypoint (which normalises a blank `CLAUDE_CODE_OAUTH_TOKEN` back to
> unset) and writes to the same `claude-home` volume; `exec` bypasses the
> entrypoint. Both work when `.env` has no token line at all.

No restart needed — every request spawns a fresh `claude -p`, which re-reads
credentials. The login is written to the `claude-home` volume and survives
`docker compose down`, restarts, and image rebuilds. Only `docker compose down -v`
destroys it (you would have to log in again).

> Do **not** try to bind-mount your host's `~/.claude` instead. On macOS the
> OAuth credentials live in the system Keychain, so there is no
> `.credentials.json` on disk to mount, and you would shadow the container's own
> state with a config written for a different machine.

**Alternative — long-lived token.** For CI or a host where the interactive flow
is awkward, mint a token on the host and pass it in instead of logging in:

```bash
claude setup-token          # requires an active subscription
cp .env.example .env        # paste the value into CLAUDE_CODE_OAUTH_TOKEN=
docker compose up -d
```

### Mode 2 — token against another endpoint

Setting `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` routes the CLI at any
Anthropic-compatible endpoint, so no subscription and no login are involved:

```dotenv
ANTHROPIC_BASE_URL=http://your-gateway:port
ANTHROPIC_AUTH_TOKEN=sk-...
ADAPTER_DEFAULT_MODEL=cc/claude-sonnet-5
```

`docker compose up -d` and it works with an empty volume. `claude auth status`
reports `authMethod: oauth_token` in this mode, versus `claude.ai` for a stored
subscription login.

Two things to get right:

- **Model names.** A gateway usually namespaces them (`cc/claude-sonnet-5`, not
  `claude-sonnet-5`). Clients must send the namespaced name, and
  `ADAPTER_DEFAULT_MODEL` covers requests that send no `model` at all. The
  hardcoded list at `GET /v1/models` is **not** updated by this setting — it
  still advertises Anthropic names, so a client that picks from that list will
  ask for a model the gateway does not have.
- **Use `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`, never both.** Claude Code
  sends both headers and endpoints reject the request; the entrypoint refuses to
  start rather than let that fail one request at a time.

> Consider whether you need this adapter at all in this mode. If the endpoint
> already speaks the Anthropic Messages API, point your client straight at it:
> you keep real streaming and `count_tokens`, and drop a subprocess per request.
> The adapter earns its place when the endpoint is *only* reachable through the
> CLI — that is, a subscription.

Point your client at `http://127.0.0.1:8082` exactly as in the sections below.

### What the compose file does

| Setting | Why |
|---|---|
| `ports: 127.0.0.1:8082:8082` | The adapter ignores the API key and has **no authentication** — never publish it on `0.0.0.0` without an authenticating reverse proxy in front. |
| `--host 0.0.0.0` in the image `CMD` | `server.py` defaults to `127.0.0.1`, which is unreachable from outside the container. Exposure is restricted at the port mapping instead. |
| `claude-home` volume on `/home/app` | Persists the login and CLI state. Mounts the whole HOME, not just `~/.claude` — Claude Code keeps state in **both** `~/.claude/` (credentials, projects) and `~/.claude.json` (config); persisting only the directory leaves a half-written state that makes the CLI emit config warnings. |
| `deploy.resources.limits` | Each request spawns a Node process and the adapter has no concurrency cap — this bounds the blast radius of a burst. |
| non-root `app` user, `cap_drop: ALL` | `server.py` is mounted read-only to the service account; `claude -p` runs with cwd `/workspace` (empty) so it can never reach the source. |

Rebuild after bumping `CLAUDE_CODE_VERSION` in `.env`:

```bash
docker compose build --no-cache && docker compose up -d
```

### Testing

```bash
./smoke-test.sh                       # against the compose service on :8082
./smoke-test.sh http://host:9000      # against somewhere else
```

It runs two stages. **Stage A** hits the HTTP surface with `curl` and no
dependencies. **Stage B** runs `smoke-test.py` — the same flows driven through
the real Anthropic SDK in a throwaway `python:3.13-slim` container attached to
the compose network. Stage B is the one that matters: Hermes talks to the
adapter through that SDK, and a hand-rolled `curl` request will not catch a
response that is shaped wrongly for it.

Covered: `/health`, `/v1/models`, non-streaming completion, SSE streaming,
`tool_use`, and the multi-turn `tool_result` round-trip. Expect this on a
healthy install:

```
8 passed, 0 failed
```

`count_tokens` reports `SKIP` — the adapter does not implement
`/v1/messages/count_tokens`, so any SDK client that calls it gets a 404. It is
recorded as a known gap rather than a failure.

Each request spawns a fresh `claude -p`, so the full run takes a couple of
minutes. A single quick check:

```bash
curl -s -X POST http://127.0.0.1:8082/v1/messages \
  -H 'content-type: application/json' -H 'x-api-key: dummy' \
  -d '{"model":"claude-sonnet-4-6","max_tokens":64,
       "messages":[{"role":"user","content":"Reply with exactly: PONG"}]}'
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `502 {"detail":"claude CLI error: Not logged in · Please run /login"}` | Not authenticated. Run the login above; check state with `docker compose run --rm adapter claude auth status`. |
| Container healthy but every request 502 | Read the `detail` field — the CLI's own message is passed through verbatim. Usually an expired login: re-run `docker compose run --rm adapter claude auth login`. |
| Logged in, but requests still fail as unauthenticated | A blank `CLAUDE_CODE_OAUTH_TOKEN=` line in `.env`. Comment it out or delete the line — an empty value is injected as `""`, which Claude Code reads as token auth and prefers over the stored login. |
| `Claude configuration file not found at /home/app/.claude.json` | The HOME volume is half-populated, usually left over from an older compose file that mounted only `/home/app/.claude`. Fix with `docker compose down -v` and log in again. |
| Long conversations fail | The prompt is passed as a command-line argument (`server.py:134`), so a large history hits `ARG_MAX` (`E2BIG`). Unrelated to Docker. |

---

## Configure Hermes to use the adapter

### Option A — environment variable (simplest)

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8082
export ANTHROPIC_API_KEY=dummy   # any non-empty string; the adapter ignores it
hermes
```

### Option B — Hermes `config.yaml`

Open `~/.hermes/config.yaml` (or wherever your config lives) and add / update
the Anthropic provider block:

```yaml
providers:
  anthropic:
    base_url: http://127.0.0.1:8082
    api_key: dummy          # required by the SDK, value is ignored by the adapter
    model: claude-opus-4-7
```

### Option C — `.env` file next to the adapter

```dotenv
ANTHROPIC_BASE_URL=http://127.0.0.1:8082
ANTHROPIC_API_KEY=dummy
```

---

## How it works

1. Hermes sends a normal `POST /v1/messages` to `localhost:8082`.
2. The adapter converts the message list + system prompt + tool definitions into
   a flat Human/Assistant dialogue.
3. It calls `claude -p <dialogue> --system-prompt <system> --tools ""
   --output-format stream-json --no-session-persistence`.
4. It parses the stream-json output, extracts the assistant text, and looks for
   `<tool_call>{…}</tool_call>` blocks in the response.
5. It rebuilds a valid Anthropic API response (or SSE stream) and returns it to
   Hermes.

### Tool use

The adapter converts Anthropic tool definitions into natural-language
instructions appended to the system prompt and teaches the model to emit tool
calls as `<tool_call>{"name": "…", "input": {…}}</tool_call>` blocks.  These
are parsed back into proper `tool_use` content blocks before the response is
returned.  Multi-turn tool loops work because Hermes sends `tool_result`
messages back, which the adapter serialises into the dialogue context.

---

## Limitations

* **No streaming from the CLI** — the adapter waits for the full CLI response,
  then fake-streams it in small chunks.  The user sees text appearing
  progressively, but there is no token-by-token latency improvement.
* **Built-in Claude Code tools are disabled** (`--tools ""`).  Only tools
  Hermes defines are available, via prompt engineering.
* **Token counts** are real when the CLI reports them; otherwise they are 0.
  Hermes should still function correctly — it does not require accurate counts.

---

## Running as a background service

### macOS (launchd)

```xml
<!-- ~/Library/LaunchAgents/com.claude.subscription-adapter.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.claude.subscription-adapter</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/claudehermessubscriptionadapter/server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/claude-adapter.log</string>
  <key>StandardErrorPath</key><string>/tmp/claude-adapter.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.claude.subscription-adapter.plist
```

### Linux (systemd)

```ini
# ~/.config/systemd/user/claude-adapter.service
[Unit]
Description=Claude CLI Subscription Adapter

[Service]
ExecStart=/usr/bin/python3 /path/to/claudehermessubscriptionadapter/server.py
Restart=always

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now claude-adapter
```