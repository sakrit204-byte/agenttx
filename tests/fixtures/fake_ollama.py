#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tests/fixtures/fake_ollama.py --- a scripted Ollama, for testing the loop.

    fake_ollama.py PORT SCRIPT.json

Speaks enough of Ollama's /api/tags and /api/chat to drive tools/agent/loop.py
through an exact sequence of turns, so the loop's own behaviour -- budgets,
repeat detection, transcript shape -- can be asserted without a model and
without a GPU. A real 7B cannot be scripted, which is precisely why the
loop's failure handling cannot be tested against one.

SCRIPT.json is a list of assistant messages, returned in order; the last
one repeats if the loop asks for more.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = []
CALLS = {"n": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                      # the test's stdout is the test's output

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            # Report every model any test might be configured to use, plus
            # whatever AGENTTX_MODEL says.
            #
            # Brain.available() refuses to run when the configured model
            # is not in this list, so a fixture that names ONE model
            # silently breaks every test the moment somebody changes the
            # default in brain.py. That happened twice: the loop exited 69
            # "model not ready" before doing anything, and the suite
            # reported a pile of missing events rather than one wrong name.
            names = ["qwen2.5:7b", "qwen2.5-coder:7b", "llama3.1:8b"]
            want = os.environ.get("AGENTTX_MODEL", "").strip()
            if want and want not in names:
                names.insert(0, want)
            self._send({"models": [{"name": n} for n in names]})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8", "replace")

        # Two modes.
        #
        # Sequential (the default) walks SCRIPT in order, which is what the
        # single-loop tests want.
        #
        # Content-aware is for the SWARM test, where several agents share
        # one fixture and a global counter would hand each of them a
        # different step of the script -- agent 1 gets the write, agent 2
        # gets the summary, and the conflict the test exists to prove never
        # happens. Deciding from the request instead makes every agent
        # behave identically no matter what order they arrive in.
        if SCRIPT and isinstance(SCRIPT[0], dict) and SCRIPT[0].get("_mode") == "content":
            by = {e.get("_when"): e for e in SCRIPT if e.get("_when")}
            if "Split this job" in body:
                msg = by.get("plan")
            elif '"role": "tool"' in body or '"role":"tool"' in body:
                msg = by.get("after_tool")
            else:
                msg = by.get("first")
            msg = {k: v for k, v in (msg or {}).items()
                   if not k.startswith("_")}
        else:
            i = min(CALLS["n"], len(SCRIPT) - 1)
            CALLS["n"] += 1
            msg = SCRIPT[i]

        self._send({"message": msg, "done": True,
                    "eval_count": 20, "prompt_eval_count": 100})


if __name__ == "__main__":
    port = int(sys.argv[1])
    with open(sys.argv[2]) as f:
        SCRIPT = json.load(f)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
