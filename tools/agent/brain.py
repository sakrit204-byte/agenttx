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
DEFAULT_MODEL = os.environ.get("AGENTTX_MODEL", "qwen2.5-coder:7b")


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL,
                 timeout: int = 300):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    # --- plumbing -----------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.host + path, data=body,
            headers={"Content-Type": "application/json"})
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
        One assistant turn. Returns Ollama's `message` object, which may
        carry `tool_calls`.

        Low temperature on purpose. This model is being asked to pick a
        tool and fill in its arguments, not to write prose; sampling
        variety there buys nothing and costs malformed arguments.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = tools
        t0 = time.time()
        out = self._post("/api/chat", payload)
        msg = out.get("message") or {}
        msg["_elapsed"] = time.time() - t0
        msg["_eval_count"] = out.get("eval_count")
        msg["_prompt_eval_count"] = out.get("prompt_eval_count")
        return msg


if __name__ == "__main__":
    import sys
    b = Brain()
    ok, detail = b.available()
    print("host :", b.host)
    print("model:", b.model)
    print("ready:", ok, "--", detail)
    if ok and len(sys.argv) > 1:
        m = b.chat([{"role": "user", "content": " ".join(sys.argv[1:])}])
        print("\n" + (m.get("content") or ""))
        if m.get("_eval_count"):
            print("\n[%d tokens in %.1fs = %.1f tok/s]"
                  % (m["_eval_count"], m["_elapsed"],
                     m["_eval_count"] / max(m["_elapsed"], 1e-6)))
