# Hermes Claude CLI Proxy

FastAPI service that exposes an Anthropic Messages-API-compatible HTTP
interface (`/v1/messages`, `/v1/models`, `/health`, `/debug/profiles`) and
routes every request through the local `claude -p` CLI, so hermes bills
against the Claude Pro/Max subscription instead of the API/overage bucket.

**This directory's `server.py` is what the repo's `Dockerfile` builds into the
image** — it is the shipped server, not a side experiment. The pre-proxy adapter
(`../server.py`, `--system-prompt`, no session reuse) is kept only as a fallback
behind `../Dockerfile.v1`. Edits here change the published image.

Runs at `http://127.0.0.1:8082` under `docker compose` (the module's own default
of 8090 applies only when running `python server.py` by hand — `CMD` passes the
port explicitly). Hermes (`/opt/hermes`) is configured to talk to it via
`providers.anthropic.base_url` in `/opt/data/config.yaml` and
`ANTHROPIC_BASE_URL` in `/opt/data/.env`.

No log file. The service writes to stdout/stderr and the Docker json-file driver
keeps it (`docker compose logs -f`, rotated 3×10MB). Do not add file logging or
a log bind-mount.

See `README.md` in this directory for full root-cause writeup, run/stop
commands, and known limitations. Read it before making changes here.

## Critical constraint — do not use `claude -p --system-prompt`

`--system-prompt <text>` REPLACES Claude Code's default system prompt and is
never cache_control-marked by the CLI — every call pays full fresh-token
price for the whole text, no matter how many times it repeats. This is what
caused the original "out of extra usage" failures (hermes always sends a
large system prompt + ~20 tool defs, even for "hi").

Always use `--append-system-prompt` instead — it adds to the default system
prompt, which IS cached, and the appended content gets folded into that same
cached block. Verified: identical 86KB system prompt via `--append-system-prompt`
+ `--resume` on the second call showed `cache_read_input_tokens` in the tens
of thousands and `cache_creation_input_tokens` near zero, vs. `--system-prompt`
which always showed `cache_creation_input_tokens: 0` (no caching at all).

If you ever touch `_run_claude()` in `server.py`, do not reintroduce
`--system-prompt` for hermes's content.

## Session/profile model

`server.py` keys a `Profile` (one persistent `claude` CLI session) by
`sha256(system_text + tools_text)`. On each request it checks whether the
incoming `messages` array is an exact-JSON prefix extension of the last one
seen for that profile:

- Yes → `--resume <session_id>`, send only the new tail messages (cheap,
  cache reads).
- No → bootstrap a new session (`--session-id <new uuid>`), send the full
  message list (full price — this is the only case that can still hit the
  "extra usage" burst limit if quota happens to be empty at that moment).

Custom hermes tools are NOT passed as native `--tools` (that flag only
accepts Claude Code's own built-in tool names) — they're flattened into the
system prompt as instructions to emit `<tool_call>{"name":...,"input":...}</tool_call>`
blocks, then parsed back out of the CLI's text output into `tool_use` content
blocks. This is a text-parsing approximation, not real structured tool use —
keep that in mind if tool-call reliability ever needs debugging.

## Gotchas learned while building this

- `claude --model <id>` accepts short aliases (`sonnet`, `opus`, `fable`) and
  some full IDs (`claude-sonnet-4-6`), but NOT every model string that looks
  valid (`claude-sonnet-5` fails on this account/CLI version) — verify a
  model string with `claude -p "hi" --model <id>` before wiring it into
  `config.yaml`.
- The "extra usage" quota is a short (~30s) shared burst bucket. Any process
  hammering `claude -p` directly (including manual debugging from a Claude
  Code session logged into the same account) competes for the same quota as
  this proxy — don't fire off rapid manual test loops while diagnosing live
  failures, it can itself cause the failures you're chasing.
- `claude-cli-adapter:8082` used to be a separate, already-deployed container
  carrying the same architecture bug (`--system-prompt`, no caching), and this
  proxy was written to bypass it. That is no longer the split: the repo's
  `Dockerfile` now builds THIS server, so the `claude-cli-adapter` image and the
  proxy are the same thing. Any still-running container from before the switch
  is the old code — rebuild it (`docker compose up -d --build`) rather than
  reasoning about two implementations.
- Session state is split across two places with different lifetimes: the
  in-memory `_profiles` map (lost on restart → one re-bootstrap per fingerprint)
  and the CLI's own transcripts under `$HOME/.claude/projects/` (kept in the
  `claude-home` volume). `--resume` needs both; losing only the first is cheap,
  losing only the second breaks resume and forces a bootstrap anyway.
- Not stateless, so it does not scale by replica: two containers behind one port
  would each hold their own profile map and thrash each other's caches.
