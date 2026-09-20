# Running it on a real project

A toy folder with three files proves nothing about this system. The
copy-on-write layer, the structure view and the commit path behave the
same on three files as on three hundred; what a real checkout tests is
whether they still behave when there are three hundred.

## Setup

In the guest:

    bash /mnt/agenttx/tools/demo/setup-repo.sh

That clones Flask into `/tmp/repo` — 236 files, 24 Python files under
`src/` — and gives it to the agent user. Any repository works:

    bash tools/demo/setup-repo.sh https://github.com/psf/requests /tmp/repo

In the app: **New chat**, set the folder to `/tmp/repo`.

## The prompt

This is written the way a 7B needs it: the goal, the two things not to do,
and the steps in order. A frontier model does not need this much
scaffolding. A small one does, and pretending otherwise just produces a
demo that fails in front of people.

```
This is the Flask source tree, checked out from git.

GOAL: every .py file under src/ must start with the line
    # SPDX-License-Identifier: BSD-3-Clause

There are two dozen such files. Do NOT edit them one at a time, and do
NOT try to fit the logic into python3 -c.

Do exactly this, in order:
1. write_file a script called add_spdx.py. It walks src/ recursively and,
   for every .py file that does not already start with that line,
   rewrites the file with the line added at the top. Have it print the
   number of files it changed.
2. run: python3 add_spdx.py
3. run: grep -rLx "# SPDX-License-Identifier: BSD-3-Clause" --include="*.py" src | wc -l
   (that prints how many .py files still lack the header; it should be 0)
4. Tell me how many files you changed.
```

Why each part is there:

| Line | What it is for |
|---|---|
| "Do NOT edit them one at a time" | a small model will otherwise try, and run out of steps around file four |
| "Do NOT ... python3 -c" | observed failure: five attempts to cram a `for` loop into `-c`, same SyntaxError each time |
| "write_file a script ... then run" | the shape that works: a real multi-line file, then execute it |
| the `grep -rLx` check | makes the agent verify instead of asserting. Its own claim is not evidence |
| "Tell me how many" | gives the turn a definite end, so it stops instead of wandering |

## What to look at afterwards

**Under the hood → File structure.** The folder as a tree, with this
layer's changes marked on it: every edited file under `src/flask/` in
amber, untouched files greyed in beside them, `.git/` and `.github/`
collapsed because nothing in them changed. At two dozen files the
question stops being "how much changed" and becomes "where", and a tree
answers that in a glance where a list does not.

**Under the hood → Live layers.** The same change as a picture: your real
folder along the bottom, the agent's private layer above it, nothing
flowing down. Click the layer for the lines, additions green, removals
red.

**The real folder.** In the guest, while the decision is still pending:

    head -1 /tmp/repo/src/flask/__init__.py

It still says `from . import json as json`. Two dozen files have been
rewritten and none of it has happened yet.

Then press Keep, and run it again.

## The no-model version

The same work with no model at all, as one shell turn — type this into
the composer with the leading `$`:

    $ python3 /usr/local/lib/agenttx/add_spdx.py

It produces exactly the same transaction, the same structure view and the
same decision. That is the control arm: when the agent does something
surprising, running the identical change by hand through the identical
machinery tells you in one step whether you are looking at the model or
the sandbox.

## Honest note on model size

`qwen2.5-coder:7b` has no tool-calling template in Ollama, so every call
arrives as text in the content field. The loop parses that, but after two
or three steps the model tends to start *describing* the next step
instead of taking it. The harness detects the common shapes of this --
repeated identical calls, repeated failures, empty replies, narration
instead of action -- and says so rather than reporting a success that
changed nothing. It cannot make a small model finish a long task.

Use a tools-native model (`qwen2.5:7b`, `llama3.1:8b`) for multi-step work
and keep the coder model for single edits:

    ollama pull qwen2.5:7b
