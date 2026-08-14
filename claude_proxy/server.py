"""
Hermes <-> Claude CLI subscription proxy.

Exposes an Anthropic Messages-API-compatible HTTP interface (/v1/messages,
/v1/models, /health) that routes every request through the local `claude -p`
CLI, so hermes bills against the Claude Pro/Max subscription instead of the
API/overage bucket.

Key finding this design is built on: `claude -p --system-prompt <text>`
REPLACES the default system prompt and disables Anthropic prompt caching for
it entirely (cache_creation_input_tokens stays 0 no matter how large the
text is). `--append-system-prompt <text>` instead ADDS to Claude Code's own
default system prompt -- which IS cached -- and the appended text gets
folded into that same cached block. Combined with reusing one claude CLI
session per (system, tools) fingerprint across requests (via --session-id /
--resume, sending only the new tail of `messages` each call), repeat
requests become cheap cache reads instead of large fresh/uncached input --
which is what was tripping this workspace's small "extra usage" bucket.

Only the very first ("bootstrap") call for a given system+tools fingerprint
pays full price; every call after that, for that fingerprint, is a cache
read. Bootstrap can still fail if the account's extra-usage bucket happens
to be empty at that moment -- this proxy cannot manufacture quota Anthropic
hasn't granted, it only avoids re-paying full price on every single turn.

Usage:
    python server.py [--port 8090] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
import uuid
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

app = FastAPI(title="Hermes Claude CLI Proxy")

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

CLAUDE_CALL_TIMEOUT_S = 280


# ---------------------------------------------------------------------------
# Per-(system,tools) session state
# ---------------------------------------------------------------------------

class Profile:
    """One persistent claude CLI session for a given (system, tools) fingerprint."""

    def __init__(self, key: str):
        self.key = key
        self.claude_session_id: str | None = None
        self.last_messages: list = []
        self.lock = asyncio.Lock()


_profiles: dict[str, Profile] = {}
_profiles_lock = asyncio.Lock()


async def _get_profile(key: str) -> Profile:
    async with _profiles_lock:
        profile = _profiles.get(key)
        if profile is None:
            profile = Profile(key)
            _profiles[key] = profile
        return profile


# ---------------------------------------------------------------------------
# Request -> CLI helpers
# ---------------------------------------------------------------------------

def _extract_system_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(
            block.get("text", "") for block in system if block.get("type") == "text"
        )
    return ""


def _tool_instructions(tools: list) -> str:
    if not tools:
        return ""
    descs = []
    for t in tools:
        descs.append(
            f"Tool name: {t['name']}\n"
            f"Description: {t.get('description', '')}\n"
            f"Input schema: {json.dumps(t.get('input_schema', {}))}"
        )
    tool_block = "\n\n".join(descs)
    return (
        "\n\nYou have access to the following tools, provided by the calling "
        "application (these are separate from your own built-in tools). "
        "When you want to call one, output ONLY a JSON object wrapped in "
        "<tool_call>...</tool_call> tags, like this:\n\n"
        "<tool_call>\n"
        '{"name": "<tool_name>", "input": {"<key>": "<value>"}}\n'
        "</tool_call>\n\n"
        "Do not write anything else on the same line as the tags. You may "
        "continue your response after the closing tag.\n\n"
        f"Available tools:\n\n{tool_block}"
    )


def _block_to_text(block: dict) -> str:
    btype = block.get("type", "")
    if btype == "text":
        return block.get("text", "")
    if btype == "tool_use":
        return (
            "<tool_call>\n"
            f'{json.dumps({"name": block["name"], "input": block.get("input", {})})}\n'
            "</tool_call>"
        )
    if btype == "tool_result":
        inner = block.get("content", "")
        if isinstance(inner, list):
            inner = " ".join(b.get("text", "") for b in inner if b.get("type") == "text")
        err = " error=true" if block.get("is_error") else ""
        return f"<tool_result id={block.get('tool_use_id', '')}{err}>{inner}</tool_result>"
    return ""


def _message_to_text(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(filter(None, (_block_to_text(b) for b in content)))
    else:
        text = str(content)
    prefix = "Human" if msg.get("role") == "user" else "Assistant"
    return f"{prefix}: {text}"


def _messages_are_prefix(prev: list, new: list) -> bool:
    if not prev or len(prev) > len(new):
        return False
    return json.dumps(prev, sort_keys=True) == json.dumps(new[: len(prev)], sort_keys=True)


# ---------------------------------------------------------------------------
# CLI invocation
# ---------------------------------------------------------------------------

async def _run_claude(
    *,
    prompt: str,
    system_prompt: str | None,
    model: str | None,
    session_id: str,
    resume: bool,
    effort: str | None,
) -> dict:
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "json",
        "--tools", "",
        "--exclude-dynamic-system-prompt-sections",
    ]
    if resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_CALL_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="claude CLI timed out")

    stdout_text = stdout_bytes.decode(errors="replace").strip()
    if proc.returncode not in (0, None):
        stderr_text = stderr_bytes.decode(errors="replace").strip()
        raise HTTPException(
            status_code=502,
            detail=f"claude CLI exited {proc.returncode}: {stderr_text or stdout_text}",
        )

    try:
        result = json.loads(stdout_text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail=f"claude CLI returned non-JSON output: {stdout_text[:2000]}",
        )

    if result.get("is_error"):
        raise HTTPException(
            status_code=502,
            detail=f"claude CLI error: {result.get('result') or json.dumps(result)[:2000]}",
        )

    return result


# ---------------------------------------------------------------------------
# Tool-call parsing / response construction
# ---------------------------------------------------------------------------

def _parse_tool_calls(raw: str) -> tuple[list[dict], str]:
    tool_blocks: list[dict] = []
    for match in _TOOL_CALL_RE.finditer(raw):
        try:
            data = json.loads(match.group(1))
            tool_blocks.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": data["name"],
                    "input": data.get("input", {}),
                }
            )
        except (json.JSONDecodeError, KeyError):
            pass
    remaining = _TOOL_CALL_RE.sub("", raw).strip()
    return tool_blocks, remaining


def _build_content_blocks(raw_text: str, tools_requested: bool) -> tuple[list[dict], str]:
    if not tools_requested:
        return [{"type": "text", "text": raw_text}], "end_turn"
    tool_calls, text = _parse_tool_calls(raw_text)
    blocks: list[dict] = []
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.extend(tool_calls)
    return blocks, ("tool_use" if tool_calls else "end_turn")


def _make_response(content_blocks: list[dict], stop_reason: str, model: str, usage: dict) -> dict:
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        },
    }


async def _sse_stream(content_blocks, stop_reason, model, usage) -> AsyncGenerator[str, None]:
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    def _send(event_type: str, data: dict) -> str:
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    yield _send(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0},
            },
        },
    )

    for i, block in enumerate(content_blocks):
        if block["type"] == "text":
            yield _send(
                "content_block_start",
                {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}},
            )
            text = block["text"]
            chunk = 32
            for start in range(0, len(text), chunk):
                yield _send(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": i,
                        "delta": {"type": "text_delta", "text": text[start:start + chunk]},
                    },
                )
                await asyncio.sleep(0)
        elif block["type"] == "tool_use":
            yield _send(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
                },
            )
            yield _send(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                },
            )
        yield _send("content_block_stop", {"type": "content_block_stop", "index": i})

    yield _send(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        },
    )
    yield _send("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def post_messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    messages: list = body.get("messages", [])
    system_raw = body.get("system", "")
    tools: list = body.get("tools", [])
    requested_model: str = body.get("model", "") or ""
    model = requested_model[3:] if requested_model.startswith("cc/") else requested_model
    stream: bool = bool(body.get("stream", False))
    effort = (body.get("output_config") or {}).get("effort")

    system_text = _extract_system_text(system_raw)
    tool_text = _tool_instructions(tools)
    full_system = (system_text + tool_text) or None

    profile_key = hashlib.sha256((system_text + "\x00" + tool_text).encode()).hexdigest()[:16]
    profile = await _get_profile(profile_key)

    async with profile.lock:
        can_resume = bool(profile.claude_session_id) and _messages_are_prefix(profile.last_messages, messages)
        if can_resume:
            delta = messages[len(profile.last_messages):]
            resume = True
            session_id = profile.claude_session_id
        else:
            delta = messages
            resume = False
            session_id = str(uuid.uuid4())

        if not delta:
            prompt_text = "(continue)"
        else:
            prompt_text = "\n\n".join(_message_to_text(m) for m in delta) + "\n\nAssistant:"

        try:
            result = await _run_claude(
                prompt=prompt_text,
                system_prompt=full_system,
                model=model or None,
                session_id=session_id,
                resume=resume,
                effort=effort,
            )
        except HTTPException:
            # Don't advance session state on failure -- next attempt should
            # retry the same bootstrap/resume rather than drifting further.
            raise

        profile.claude_session_id = session_id
        profile.last_messages = messages

    raw_text = result.get("result", "")
    usage = result.get("usage", {})
    content_blocks, stop_reason = _build_content_blocks(raw_text, bool(tools))
    response_model = requested_model or model

    if stream:
        return StreamingResponse(
            _sse_stream(content_blocks, stop_reason, response_model, usage),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return JSONResponse(_make_response(content_blocks, stop_reason, response_model, usage))


@app.get("/v1/models")
async def list_models():
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {"id": "claude-sonnet-4-6", "object": "model"},
                {"id": "claude-opus-4-7", "object": "model"},
                {"id": "claude-haiku-4-5-20251001", "object": "model"},
            ],
        }
    )


@app.get("/health")
async def health():
    return {"status": "ok", "profiles": len(_profiles)}


@app.get("/debug/profiles")
async def debug_profiles():
    return {
        key: {
            "claude_session_id": p.claude_session_id,
            "message_count": len(p.last_messages),
        }
        for key, p in _profiles.items()
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hermes Claude CLI Proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    print(f"Starting proxy on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
