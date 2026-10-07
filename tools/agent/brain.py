#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/agent/brain.py --- the model behind the agents.

One small HTTP client for Ollama, and nothing else. It is a separate file
because everything above it -- the loop, the swarm, the transaction
plumbing -- must not care which model is answering, and because the model
is the one piece we want to be able to swap without touching any of the
rest.

WHERE THE MODEL RUNS, AND WHY IT IS NOT IN THE GUEST.

The guest has 6G and four vCPUs and is also running the kernel under test.
Putting a 7B model in there would mean the thing being measured and the
thing doing the work are competing for the same memory, and an OOM inside
the sandbox looks exactly like a transaction bug -- we have already been
burned by that class of confusion. So Ollama runs on the host and the
agents reach it across QEMU's user network at 10.0.2.2, which the guest
already has a default route to.

COST: there is none. This is a local model on local hardware. No API key
is read, none is sent, and nothing bills.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# 10.0.2.2 is the host as seen from inside QEMU user-mode networking. On
# the host itself that address does not resolve to anything useful, so the
# env override is what tests and host-side tools use.
DEFAULT_HOST = os.environ.get("AGENTTX_OLLAMA", "http://10.0.2.2:11434")
# qwen2.5:7b, not qwen2.5-coder:7b.
#
# The coder variant writes better Python and has NO tool-calling template
# in Ollama, so every call arrives as text in the content field. The loop
# parses that, but after two or three steps the model starts describing
# the next step instead of taking it -- on the same task, the coder model
# stopped after writing the script and narrated "2. run: python3
# add_spdx.py", while this one wrote it, ran it, verified the result with
# grep and reported the count, in five steps.
#
# Tool use is the job here. Being slightly better at prose about code is
# not worth being unable to finish.
DEFAULT_MODEL = os.environ.get("AGENTTX_MODEL", "qwen2.5:7b")


# Which API dialect to speak.
#
# "ollama"  a local Ollama at AGENTTX_OLLAMA
# "openai"  anything that speaks OpenAI /chat/completions -- which is most
#           things, including several providers with a genuine free tier.
#
# The point of the split is not vendor shopping. A harness whose results
# depend on one provider's availability is a harness whose results expire,
# and a paper that says "we used a 7B" is weaker than one that says "the
# same measurement, across four models of different capability". The
# transaction machinery does not care which brain is answering and this
# keeps it that way.
DEFAULT_PROVIDER = os.environ.get("AGENTTX_PROVIDER", "ollama").lower()
DEFAULT_BASE_URL = os.environ.get("AGENTTX_BASE_URL", "")
DEFAULT_API_KEY = os.environ.get("AGENTTX_API_KEY", "")


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 timeout: int = 300, provider: str = DEFAULT_PROVIDER,
                 api_key: str = DEFAULT_API_KEY):
        self.provider = (provider or "ollama").lower()
        base = DEFAULT_BASE_URL if self.provider == "openai" else host
        self.host = (base or host).rstrip("/")
        self.model = model
        self.timeout = timeout
        self.api_key = api_key

    # --- plumbing -----------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(self.host + path, data=body,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise BrainError("%s %s: %s"
                             % (path, e.code, e.read()[:400].decode("utf-8",
                                                                    "replace")))
        except urllib.error.URLError as e:
            raise BrainError(
                "cannot reach the model at %s (%s).\n"
                "On the host:  ollama serve   (and OLLAMA_HOST=0.0.0.0)\n"
                "From the guest the host is 10.0.2.2." % (self.host, e.reason))

    def available(self) -> tuple[bool, str]:
        if self.provider == "openai":
            if not self.host:
                return False, ("set AGENTTX_BASE_URL to the provider's "
                               "OpenAI-compatible endpoint")
            if not self.api_key:
                return False, "set AGENTTX_API_KEY"
            # Not probed. A /models call costs a request against free-tier
            # quota and still would not prove this model answers; the real
            # check is the first chat, whose error already says what went
            # wrong.
            return True, "%s via %s" % (self.model, self.host)
        try:
            req = urllib.request.Request(self.host + "/api/tags")
            with urllib.request.urlopen(req, timeout=10) as r:
                tags = json.loads(r.read().decode())
            names = [m.get("name", "") for m in tags.get("models", [])]
            if not names:
                return False, "Ollama is up but has no models pulled"
            # Ollama reports "qwen2.5-coder:7b"; accept a bare name too.
            if self.model in names or any(
                    n.split(":")[0] == self.model.split(":")[0] for n in names):
                return True, ", ".join(names)
            return False, ("model %s not pulled; have: %s"
                           % (self.model, ", ".join(names)))
        except Exception as e:
            return False, str(e)

    # --- the one call the loop makes ----------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.1) -> dict:
        """
        One assistant turn. Returns a message object, which may carry
        `tool_calls`, in Ollama's shape regardless of who answered.

        Low temperature on purpose. This model is being asked to pick a
        tool and fill in its arguments, not to write prose; sampling
        variety there buys nothing and costs malformed arguments.
        """
        t0 = time.time()
        if self.provider == "openai":
            msg = self._chat_openai(messages, tools, temperature)
        else:
            msg = self._chat_ollama(messages, tools, temperature)
        msg["_elapsed"] = time.time() - t0
        return msg

    def _chat_ollama(self, messages, tools, temperature) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        out = self._post("/api/chat", payload)
        msg = out.get("message") or {}
        msg["_eval_count"] = out.get("eval_count")
        msg["_prompt_eval_count"] = out.get("prompt_eval_count")
        return msg

    def _chat_openai(self, messages, tools, temperature) -> dict:
        """
        Speak OpenAI /chat/completions, and translate the answer back.

        Two shape differences matter and both have bitten real loops:

          - a tool message needs the id of the call it answers. Ollama
            does not care; OpenAI rejects the whole request without it.
            The loop keeps tool results in Ollama's shape, so the ids are
            reattached here from the preceding assistant turn.
          - `arguments` comes back as a JSON STRING, not an object.
            tools.call() already copes with that, so it is passed through
            rather than parsed here -- one place that handles the mess.
        """
        conv, pending_ids = [], []
        for m in messages:
            role = m.get("role")
            if role == "tool":
                # Answer the calls in order; a tool reply with no id to
                # answer is dropped rather than sent, because the request
                # would be rejected whole and the turn would look like a
                # model failure.
                if not pending_ids:
                    continue
                conv.append({"role": "tool",
                             "tool_call_id": pending_ids.pop(0),
                             "content": str(m.get("content", ""))})
                continue
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for i, c in enumerate(m["tool_calls"]):
                    fn = c.get("function") or {}
                    cid = c.get("id") or ("call_%d" % i)
                    pending_ids.append(cid)
                    args = fn.get("arguments")
                    if not isinstance(args, str):
                        args = json.dumps(args or {})
                    calls.append({"id": cid, "type": "function",
                                  "function": {"name": fn.get("name"),
                                               "arguments": args}})
                conv.append({"role": "assistant",
                             "content": m.get("content") or "",
                             "tool_calls": calls})
                continue
            conv.append({"role": role, "content": m.get("content", "")})

        payload = {"model": self.model, "messages": conv,
                   "temperature": temperature}
        if tools:
            payload["tools"] = tools
        out = self._post("/chat/completions", payload)
        choices = out.get("choices") or []
        if not choices:
            raise BrainError("no reply: %s" % json.dumps(out)[:300])
        m = choices[0].get("message") or {}
        usage = out.get("usage") or {}
        return {
            "role": "assistant",
            "content": m.get("content") or "",
            "tool_calls": m.get("tool_calls") or [],
            "_eval_count": usage.get("completion_tokens"),
            "_prompt_eval_count": usage.get("prompt_tokens"),
        }


if __name__ == "__main__":
    import sys
    b = Brain()
    ok, detail = b.available()
    print("provider:", b.provider)
    print("host    :", b.host)
    print("model   :", b.model)
    print("ready   :", ok, "--", detail)
    if ok and len(sys.argv) > 1:
        m = b.chat([{"role": "user", "content": " ".join(sys.argv[1:])}])
        print("\n" + (m.get("content") or ""))
        if m.get("_eval_count"):
            print("\n[%d tokens in %.1fs = %.1f tok/s]"
                  % (m["_eval_count"], m["_elapsed"],
                     m["_eval_count"] / max(m["_elapsed"], 1e-6)))
