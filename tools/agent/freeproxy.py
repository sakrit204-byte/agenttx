#!/usr/bin/env python3
"""
A local, free-tier-only shim between the agent and OpenRouter.

WHY THIS EXISTS AT ALL, rather than pointing the brain straight at the
provider.

1. THE REASONING CHANNEL HAS TO STAY NARROW.

   An agent inside a transaction still has to talk to its model, so the
   channel it reasons over must be exempt from the interception. With a
   local model that exemption is one port: --infra-port 11434, and
   nothing else on the machine is affected. A remote provider speaks
   HTTPS on 443, and exempting 443 would exempt almost every outbound
   effect an agent could have -- which is the entire thing the
   transaction exists to hold. So the agent keeps talking to a local
   port, and this process does the remote call from outside the
   transaction, where it is nobody's effect to roll back.

2. THE KEY STAYS OUT OF THE SANDBOX.

   The agent never sees the API key. It lives in this process, on the
   host. An agent that goes wrong cannot spend it, leak it into a file
   that later gets committed, or print it into a transcript.

3. FREE MEANS VERIFIED FREE, NOT FREE BY NAMING CONVENTION.

   The ":free" suffix is a convention and conventions drift. At startup
   we ask the provider what the model actually costs and refuse to run
   unless both the prompt and completion prices are exactly zero. The
   suffix is checked too, as a second lock. A request for any other
   model is rejected here and never leaves the machine.

   This cannot make an account safe on its own. The only complete
   guarantee is no payment method on the account: a provider with no
   card on file has nothing to charge. This process is the second line.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"

# A free tier is a shared pool, and a shared pool runs out. Both Gemma
# free variants answered 429 "temporarily rate-limited upstream" while
# five other zero cost models answered immediately. A harness that dies
# because one pool is busy is not usable, so we keep an ordered list and
# walk it. EVERY entry is price-verified at startup, so falling back can
# never fall back onto a bill.
FALLBACKS = [
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "cohere/north-mini-code:free",
]

STATE = {"key": "", "model": DEFAULT_MODEL, "models": [],
         "calls": 0, "refused": 0, "fellback": 0}


def price_of(model: str, key: str):
    """Ask the provider what this model costs. Returns (prompt, completion)."""
    req = urllib.request.Request(
        UPSTREAM + "/models",
        headers={"Authorization": "Bearer " + key} if key else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
    for m in body.get("data", []):
        if m.get("id") == model:
            p = m.get("pricing") or {}
            return float(p.get("prompt", "1")), float(p.get("completion", "1"))
    raise SystemExit("freeproxy: provider does not list a model called %r.\n"
                     "           Check the exact id on the model's page."
                     % model)


def verify_free(model: str, key: str) -> None:
    if not model.endswith(":free"):
        raise SystemExit(
            "freeproxy: refusing %r.\n"
            "           Only ':free' model ids are allowed. The paid twin of\n"
            "           a free model has the SAME name without the suffix,\n"
            "           which is exactly how an accidental bill happens."
            % model)
    prompt, completion = price_of(model, key)
    if prompt != 0.0 or completion != 0.0:
        raise SystemExit(
            "freeproxy: refusing %r -- the provider prices it at\n"
            "           %s per prompt token and %s per completion token.\n"
            "           This proxy only runs models that cost exactly zero."
            % (model, prompt, completion))
    print("freeproxy: %s verified free (prompt %s, completion %s)"
          % (model, prompt, completion), flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):      # quiet; we print what matters
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Ollama's probe, so existing reachability checks keep working.
        if self.path.rstrip("/") in ("/api/tags", "/v1/models", "/models"):
            self._send(200, {"models": [{"name": STATE["model"]}],
                             "data": [{"id": STATE["model"]}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": "only /chat/completions is proxied"})
            return

        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"error": "body is not JSON"})
            return

        # The guard. A caller asking for a model outside the verified
        # free list is refused HERE, before any byte leaves the machine.
        asked = payload.get("model") or STATE["model"]
        if asked not in STATE["models"]:
            STATE["refused"] += 1
            print("freeproxy: REFUSED a request for %r; allowed: %s"
                  % (asked, ", ".join(STATE["models"])), flush=True)
            self._send(403, {"error": {
                "message": "freeproxy allows only these zero cost models: %s"
                           % ", ".join(STATE["models"]),
                "type": "model_not_allowed"}})
            return

        # Try the asked-for model, then the rest of the verified list.
        order = [asked] + [m for m in STATE["models"] if m != asked]
        last = None
        for i, model in enumerate(order):
            payload["model"] = model
            req = urllib.request.Request(
                UPSTREAM + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + STATE["key"],
                         # OpenRouter asks callers to identify themselves.
                         "HTTP-Referer": "https://github.com/agenttx",
                         "X-Title": "AgentTx"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    out = json.loads(r.read().decode())
                STATE["calls"] += 1
                if i:
                    STATE["fellback"] += 1
                    print("freeproxy: %s was unavailable; answered with %s"
                          % (asked, model), flush=True)
                self._send(200, out)
                return
            except urllib.error.HTTPError as e:
                last = (e.code, e.read().decode()[:300])
                # A busy shared pool is the case worth walking past. A
                # 400 or 404 is our own mistake and retrying elsewhere
                # would only hide it.
                if e.code not in (429, 502, 503):
                    break
            except Exception as e:                # noqa: BLE001
                last = (502, str(e))

        code, detail = last or (502, "no upstream answered")
        print("freeproxy: every free model failed; last %s: %s"
              % (code, detail), flush=True)
        self._send(code, {"error": {"message": detail}})


def main():
    ap = argparse.ArgumentParser(
        description="free-tier-only local shim to OpenRouter")
    ap.add_argument("--model", default=os.environ.get(
        "AGENTTX_FREE_MODEL", DEFAULT_MODEL))
    # 11435, not 11434: Ollama owns 11434 and the point of the
    # provider split is that both brains can be up at once, so the same
    # task can be run against each without reconfiguring anything.
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument("--fallback", default=",".join(FALLBACKS),
                    help="comma separated zero cost models to try when the "
                         "primary pool is busy; each is price-verified too")
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 so the guest reaches it at 10.0.2.2")
    ap.add_argument("--key-file", default=os.path.expanduser(
        "~/.agenttx-openrouter-key"))
    a = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key and os.path.exists(a.key_file):
        key = open(a.key_file).read().strip()
    if not key:
        raise SystemExit(
            "freeproxy: no API key.\n"
            "           Put it in %s (chmod 600) or set OPENROUTER_API_KEY.\n"
            "           Get one at https://openrouter.ai/keys -- and do not\n"
            "           add a payment method if you want a hard zero spend\n"
            "           guarantee." % a.key_file)

    wanted = [a.model] + [m for m in (a.fallback or "").split(",") if m]
    for m in wanted:
        verify_free(m, key)          # refuses to start on anything priced
    STATE["key"], STATE["model"], STATE["models"] = key, a.model, wanted

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("freeproxy: listening on %s:%d -> %s"
          % (a.host, a.port, UPSTREAM), flush=True)
    print("freeproxy: the guest reaches this at http://10.0.2.2:%d "
          "(10.0.2.2 under QEMU user networking)" % a.port, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nfreeproxy: %d call(s) proxied, %d refused"
              % (STATE["calls"], STATE["refused"]), flush=True)


if __name__ == "__main__":
    sys.exit(main())
