#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t13_providers.sh --- the OpenAI dialect, and the translation
# that makes it interchangeable with Ollama.
#
# The loop keeps its conversation in Ollama's shape. Talking to anything
# OpenAI-compatible means translating on the way out, and two differences
# are not cosmetic:
#
#   - a tool reply must name the id of the call it answers. Ollama does
#     not care; OpenAI rejects the ENTIRE request without it, so the
#     failure arrives as "the model refused" rather than "the message was
#     malformed".
#   - `arguments` comes back as a JSON string rather than an object.
#
# Tier 0: pure translation, no network, no model, nothing spent.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v python3 >/dev/null || { echo "needs python3"; exit 77; }

python3 - "$REPO" <<'PY'
import json, os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "tools", "agent"))
import brain

fail = 0
def ok(m):  print("PASS:", m)
def bad(m):
    global fail
    print("FAIL:", m); fail += 1

sent = {}
def fake_post(self, path, payload):
    sent["path"] = path
    sent["payload"] = payload
    return {"choices": [{"message": {"content": "done", "tool_calls": []}}],
            "usage": {"completion_tokens": 7, "prompt_tokens": 11}}
brain.Brain._post = fake_post

b = brain.Brain(provider="openai", model="m", api_key="k")
b.host = "https://example.invalid/v1"

# --- config -----------------------------------------------------------
okk, detail = b.available()
(ok if okk else bad)("an openai provider with a key is considered ready")
nokey = brain.Brain(provider="openai", model="m", api_key="")
nokey.host = "https://example.invalid/v1"
okk, detail = nokey.available()
(ok if (not okk and "API_KEY" in detail) else bad)(
    "a missing key is reported, not discovered at request time")

# --- the round trip the loop actually performs ------------------------
msgs = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "do it"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"function": {"name": "list_dir",
                                  "arguments": {"path": "."}}}]},
    {"role": "tool", "content": "a.py b.py"},
]
b.chat(msgs, tools=[{"type": "function",
                     "function": {"name": "list_dir", "parameters": {}}}])
conv = sent["payload"]["messages"]

(ok if sent["path"] == "/chat/completions" else bad)("posts to /chat/completions")

asst = [m for m in conv if m["role"] == "assistant"]
tool = [m for m in conv if m["role"] == "tool"]
if not asst or not tool:
    bad("the assistant/tool pair did not survive translation")
else:
    call_id = asst[0]["tool_calls"][0].get("id")
    if call_id and tool[0].get("tool_call_id") == call_id:
        ok("the tool reply names the call it answers (%s)" % call_id)
    else:
        bad("tool_call_id missing or mismatched: %r vs %r"
            % (call_id, tool[0].get("tool_call_id")))

    args = asst[0]["tool_calls"][0]["function"]["arguments"]
    if isinstance(args, str) and json.loads(args) == {"path": "."}:
        ok("arguments are serialised to a JSON string, as the API wants")
    else:
        bad("arguments were sent as %r" % type(args).__name__)

# A tool reply with nothing to answer must be DROPPED, not sent: the
# request would be rejected whole and read as a model failure.
sent.clear()
b.chat([{"role": "user", "content": "x"},
        {"role": "tool", "content": "orphan"}])
if any(m["role"] == "tool" for m in sent["payload"]["messages"]):
    bad("an orphan tool reply was sent and would reject the request")
else:
    ok("an orphan tool reply is dropped rather than sent")

# --- the answer comes back in Ollama's shape --------------------------
sent.clear()
m = b.chat([{"role": "user", "content": "x"}])
if m.get("role") == "assistant" and m.get("content") == "done" \
        and m.get("tool_calls") == []:
    ok("the reply is translated back into the shape the loop expects")
else:
    bad("reply shape is wrong: %r" % m)
(ok if m.get("_eval_count") == 7 else bad)("token counts are carried across")

# --- ollama stays the default ----------------------------------------
d = brain.Brain()
(ok if d.provider == "ollama" else bad)(
    "ollama is still the default provider (%s)" % d.provider)

sys.exit(fail)
PY
