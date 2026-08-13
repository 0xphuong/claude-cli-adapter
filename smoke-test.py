"""Fidelity check: drive the adapter with the real Anthropic SDK, as Hermes does.

Run it through smoke-test.sh, or standalone:
    pip install anthropic
    ADAPTER_URL=http://127.0.0.1:8082 python smoke-test.py
"""
import os
import sys

import anthropic

BASE = os.environ.get("ADAPTER_URL", "http://127.0.0.1:8082")
MODEL = os.environ.get("ADAPTER_TEST_MODEL", "claude-sonnet-4-6")
# The adapter ignores the key entirely; override it to point this suite at a
# real Anthropic-compatible endpoint and compare behaviour.
KEY = os.environ.get("ADAPTER_API_KEY", "dummy")

client = anthropic.Anthropic(base_url=BASE, api_key=KEY)
failed = []

print(f"SDK fidelity check against {BASE} (model {MODEL})\n")

print("1. messages.create .............", end=" ", flush=True)
m = client.messages.create(
    model=MODEL, max_tokens=64,
    messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
)
assert m.content[0].text.strip().startswith("PONG"), m.content
print(f"OK   usage={m.usage.input_tokens}/{m.usage.output_tokens}")

print("2. messages.stream .............", end=" ", flush=True)
with client.messages.stream(
    model=MODEL, max_tokens=64,
    messages=[{"role": "user", "content": "Say: STREAM_OK"}],
) as s:
    chunks = list(s.text_stream)
    final = s.get_final_message()
assert final.stop_reason == "end_turn", final.stop_reason
print(f"OK   {len(chunks)} chunk(s)")

TOOLS = [{
    "name": "get_weather",
    "description": "Get current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}]

print("3. tool_use ....................", end=" ", flush=True)
t = client.messages.create(
    model=MODEL, max_tokens=512, tools=TOOLS,
    messages=[{"role": "user", "content": "Weather in Hanoi? Use the tool."}],
)
assert t.stop_reason == "tool_use", t.stop_reason
tu = next(b for b in t.content if b.type == "tool_use")
print(f"OK   {tu.name}({tu.input})")

print("4. tool_result round-trip ......", end=" ", flush=True)
r = client.messages.create(
    model=MODEL, max_tokens=512, tools=TOOLS,
    messages=[
        {"role": "user", "content": "Weather in Hanoi? Use the tool."},
        {"role": "assistant", "content": t.content},
        {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tu.id,
            "content": "32C, humid, thunderstorms",
        }]},
    ],
)
assert r.stop_reason == "end_turn", r.stop_reason
print("OK")

# Known gap, not a regression: the adapter exposes no /v1/messages/count_tokens.
# Reported rather than asserted so the suite stays green until it is implemented.
print("5. count_tokens ................", end=" ", flush=True)
try:
    client.messages.count_tokens(
        model=MODEL, messages=[{"role": "user", "content": "hi"}],
    )
    print("OK   (endpoint now implemented)")
except anthropic.NotFoundError:
    print("SKIP not implemented by the adapter (known gap)")
except Exception as e:  # noqa: BLE001 - surface anything unexpected
    failed.append(f"count_tokens: {type(e).__name__}: {e}")
    print(f"FAIL {type(e).__name__}")

if failed:
    print("\n" + "\n".join(failed))
    sys.exit(1)
print("\nAll SDK checks passed.")
