# Which brain to run, and what it costs

The transaction machinery does not care which model is answering. This is
the list of ways to point it at one without paying anything.

## 1. Claude Code on your existing subscription — recommended

Already built: `mode="claude"` runs `tools/harness/tx-agent.py`, which
drives Claude Code headless with `--output-format stream-json` and parses
the result into the same events the local loop emits.

It authenticates with the **subscription you already have**, not an API
key. `tx-agent.py` strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
from the child environment precisely so a stray export cannot silently
move a run onto metered billing.

The sandbox agent has to be signed in **inside the guest**, not on your
host — the `agent` user does not exist on the host:

    sshpass -p agenttx ssh -p 2222 root@127.0.0.1
    su - agent
    claude setup-token

Then pick it per run (`mode="claude"`), or from the app.

**The honest caveat.** A subscription has rate limits, and a 3-agent swarm
burns turns quickly. For interactive work and for the demos this is
comfortably enough. For the *measurement* work — the thing the paper rests
on, which wants dozens of runs across several task shapes — you will hit
limits. Plan the local model for volume and the frontier model for the
runs whose quality matters.

There is also a methodological argument for this one: the 2.9% gate figure
was measured from **Claude Code traces**. Driving Claude Code keeps the
evaluation and the measurement on the same agent.

## 2. Any OpenAI-compatible endpoint

`tools/agent/brain.py` speaks `/chat/completions` as well as Ollama:

    AGENTTX_PROVIDER=openai
    AGENTTX_BASE_URL=<provider's OpenAI-compatible base URL>
    AGENTTX_API_KEY=<key>
    AGENTTX_MODEL=<model id>

Several providers have a real free tier — Google AI Studio, GitHub Models,
Groq, Cerebras, OpenRouter's `:free` models among them. Limits and terms
change often enough that quoting numbers here would be wrong within a
month: check the provider's current free-tier page, and check whether it
wants a card, before pointing a swarm at it.

Verify any endpoint first. Ollama's own OpenAI-compatible endpoint is a
free way to test the plumbing without touching a provider at all:

    AGENTTX_PROVIDER=openai \
    AGENTTX_BASE_URL=http://127.0.0.1:11434/v1 \
    AGENTTX_API_KEY=ollama \
    AGENTTX_MODEL=qwen2.5:7b \
    python3 tools/agent/brain.py "say hi"

## 3. Local, which is the default

`qwen2.5:7b` on Ollama. Free, offline, unlimited, and demonstrably able to
finish a 25-file refactor (`docs/demo.md`). Slower and less accurate than
a frontier model, which is the trade.

Use `qwen2.5:7b` rather than `qwen2.5-coder:7b`: the coder variant writes
better Python and has no tool-calling template, so it narrates the next
step instead of taking it.

## What not to do

Shared or leaked keys, reverse-engineered chat endpoints, and rotating
free trials are all against the providers' terms, and a result obtained
that way is not one you can put in a paper — the method has to be
reproducible by whoever reviews it.

If you need frontier-model volume for the evaluation, the legitimate route
is a research/education credit programme. Anthropic, OpenAI and Google all
run them, a university project is exactly the intended case, and the lead
time is weeks rather than days — so apply before you need it.
