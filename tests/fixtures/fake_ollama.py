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
            self._send({"models": [{"name": "qwen2.5-coder:7b"}]})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        i = min(CALLS["n"], len(SCRIPT) - 1)
        CALLS["n"] += 1
        self._send({"message": SCRIPT[i], "done": True,
                    "eval_count": 20, "prompt_eval_count": 100})


if __name__ == "__main__":
    port = int(sys.argv[1])
    with open(sys.argv[2]) as f:
        SCRIPT = json.load(f)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
