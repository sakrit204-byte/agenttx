#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/agent/tools.py --- what an agent is allowed to do, and the doing of it.

Every one of these runs with the process's cwd inside a transaction's
merged overlay. That is the whole trick: the agent calls open() and
write() like any program, the kernel puts the result in a copy-on-write
layer, and nothing reaches the real directory until somebody commits. The
tool implementations therefore contain no sandboxing logic of their own --
there is nothing here that tries to decide whether a write is safe,
because that is not where the decision lives.

What they DO contain is containment of a different kind: a 7B model will
cheerfully pass an absolute path, a path with .. in it, or a 40MB file to
read. Those are not attacks, they are ordinary small-model mistakes, and
each one would either escape the transaction or blow up the context
window. So every path is resolved and checked against the root, and every
result is truncated.
"""
from __future__ import annotations

import json
import os
import subprocess

MAX_READ = 60_000          # bytes returned from one read_file
MAX_OUT = 8_000            # bytes returned from one command
MAX_LIST = 400             # entries returned from one list_dir


class ToolError(Exception):
    """Something the model did wrong and can be told about."""


def _resolve(root: str, path: str) -> str:
    """
    Resolve `path` inside `root`, refusing anything that leaves it.

    A small model produces "/etc/passwd" and "../../secrets" routinely --
    not maliciously, just from having seen a lot of absolute paths. Inside
    the transaction those reads would even succeed, because the overlay
    only covers the working directory; everything else is the real
    filesystem. So the boundary has to be enforced here as well as by the
    kernel.
    """
    if not path or path in (".", "./"):
        return root
    p = path if os.path.isabs(path) else os.path.join(root, path)
    real = os.path.realpath(p)
    rroot = os.path.realpath(root)
    if real != rroot and not real.startswith(rroot + os.sep):
        raise ToolError(
            "path %r is outside the folder you may work in (%s). "
            "Use paths relative to it." % (path, rroot))
    return real


# --- the tools ---------------------------------------------------------
def list_dir(root: str, path: str = ".") -> str:
    d = _resolve(root, path)
    if not os.path.isdir(d):
        raise ToolError("%s is not a directory" % path)
    out = []
    for name in sorted(os.listdir(d))[:MAX_LIST]:
        full = os.path.join(d, name)
        if os.path.isdir(full):
            out.append(name + "/")
        else:
            try:
                out.append("%s (%d bytes)" % (name, os.path.getsize(full)))
            except OSError:
                out.append(name)
    return "\n".join(out) if out else "(empty directory)"


def read_file(root: str, path: str) -> str:
    f = _resolve(root, path)
    if not os.path.isfile(f):
        raise ToolError("%s does not exist" % path)
    with open(f, "rb") as fh:
        data = fh.read(MAX_READ + 1)
    text = data[:MAX_READ].decode("utf-8", "replace")
    if len(data) > MAX_READ:
        text += "\n... (truncated)"
    return text


def write_file(root: str, path: str, content: str) -> str:
    f = _resolve(root, path)
    os.makedirs(os.path.dirname(f) or root, exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(content)
    return "wrote %s (%d bytes)" % (path, len(content.encode()))


def edit_file(root: str, path: str, old: str, new: str) -> str:
    """
    Replace an exact string. Refuses when it is not unique.

    A small model reaches for edit_file with a one-line `old` that appears
    six times, and a replace-all would quietly corrupt the file in five
    places it never looked at. Making that an error it can see and retry
    from is worth far more than the convenience.
    """
    f = _resolve(root, path)
    if not os.path.isfile(f):
        raise ToolError("%s does not exist" % path)
    if not old:
        # An empty needle "occurs" between every pair of characters, so
        # str.count returned len(file)+1 and the model was told its text
        # appeared 2073 times in a 2072-byte file. A small model reaches
        # for old:"" when it wants to PREPEND something, so say what to do
        # instead of reporting an absurd number.
        raise ToolError(
            "old must not be empty. To add something at the top of a file, "
            "read_file it and write_file the whole new contents. To change "
            "many files at once, use run with a script.")
    with open(f, encoding="utf-8") as fh:
        body = fh.read()
    n = body.count(old)
    if n == 0:
        raise ToolError("that exact text is not in %s" % path)
    if n > 1:
        raise ToolError(
            "that text appears %d times in %s; include more surrounding "
            "lines so it matches exactly once" % (n, path))
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(body.replace(old, new, 1))
    return "edited %s" % path


# --- exclusive claims, and the deadlock they make possible -------------
#
# Everything above this line is deadlock-free by construction: each agent
# works in a private copy-on-write layer, never waits for another's data,
# and conflicts are settled at commit (docs/deadlock.md §2.2). That is a
# designed property, and it is why no amount of file editing by any number
# of agents has ever produced a wait-for edge.
#
# But not everything can be done optimistically. An irrevocable effect --
# a deploy slot, an outbound send, a migration against a live database --
# cannot be performed speculatively by two agents and reconciled
# afterwards, because there is nothing to reconcile. Those need EXCLUSIVE
# access, which means a transaction that waits, which means a wait-for
# graph, which means cycles. docs/deadlock.md §2.3 calls this the deadlock
# AgentTx can actually suffer, and it is a direct consequence of the
# mechanism's own design rather than a bug in it.
#
# `claim` is that: a named resource an agent must hold exclusively. The
# agent blocks; the block is declared to the kernel as a wait-for edge;
# the kernel runs cycle detection on insert and aborts a victim.

# NOT under /run/agenttx.
#
# That directory is 0700 root -- it holds the session state that decides
# whether a transaction commits -- so the agent cannot traverse into it,
# and a claims directory inside it was unreachable no matter what mode it
# had. It was mode 1777 and still invisible, because traversal needs
# execute on every component of the path.
#
# Same answer as the thread transcripts: give it its own root rather than
# relaxing a directory that is strict on purpose.
CLAIM_DIR = "/run/agenttx-claims"


def _claim_path(resource: str) -> str:
    safe = "".join(c for c in resource if c.isalnum() or c in "-_.")[:64]
    if not safe:
        raise ToolError("a resource needs a name")
    return os.path.join(CLAIM_DIR, safe)


def _first_claim_barrier() -> None:
    """
    Hold after the FIRST claim until every agent has one.

    This is demo scaffolding and worth naming as such. A three-way
    deadlock needs all three agents holding one resource before any of
    them asks for a second; if one agent gets both before the others have
    started, there is no cycle and nothing to detect. Without this the
    deadlock is a race that usually fires -- with it, it always does.

    The deadlock is equally real either way. This fixes the interleaving,
    not the mechanism. Off unless AGENTTX_CLAIM_BARRIER is set, which the
    orchestrator only sets when it is running more than one agent.
    """
    import time as _t

    try:
        n = int(os.environ.get("AGENTTX_CLAIM_BARRIER", "0"))
    except ValueError:
        n = 0
    if n < 2:
        return
    deadline = _t.time() + 30
    while _t.time() < deadline:
        try:
            held = sum(1 for f in os.listdir(CLAIM_DIR) if ".want." not in f)
        except OSError:
            return
        if held >= n:
            return
        _t.sleep(0.2)


def claim(root: str, resource: str, timeout: float = 120.0) -> str:
    """
    Take exclusive hold of a named resource, waiting if someone has it.

    Blocking is the point. The `.want` file is how a blocked agent tells
    the orchestrator who it is waiting for: the orchestrator runs as root,
    holds /dev/agenttx, and turns that into a real TX_IOC_WAIT edge. The
    agent cannot do it itself -- /dev/agenttx is root-only, deliberately,
    because anything that can open it can commit and abort transactions
    that are not its own.
    """
    import time as _t

    me = os.environ.get("AGENTTX_TX_ID", "").strip()
    if not me:
        raise ToolError("not inside a transaction, so nothing can be claimed")
    # NOT created here. /run/agenttx is root-only and the agent is not
    # root; the orchestrator makes this directory before starting anyone.
    # Trying and failing produced a PermissionError that read like a bug
    # in the claim itself.
    if not os.path.isdir(CLAIM_DIR):
        raise ToolError(
            "there is no shared-resource registry in this sandbox, so "
            "nothing needs claiming here. Carry on without it.")
    path = _claim_path(resource)
    want = "%s.want.%s" % (path, me)
    deadline = _t.time() + timeout

    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, me.encode())
            os.close(fd)
            try:
                os.unlink(want)
            except OSError:
                pass
            _first_claim_barrier()
            return "claimed %s (exclusive, until this transaction ends)" % resource
        except FileExistsError:
            try:
                with open(path) as f:
                    holder = f.read().strip()
            except OSError:
                continue                      # released under us; retry
            if holder == me:
                return "%s is already yours" % resource

            # Tell the orchestrator who is blocking us. It declares the
            # edge; if that closes a cycle the kernel aborts one of us and
            # this process simply dies mid-wait, which is the correct
            # outcome and needs no handling here.
            try:
                with open(want, "w") as f:
                    f.write(holder)
            except OSError:
                pass

            if _t.time() > deadline:
                try:
                    os.unlink(want)
                except OSError:
                    pass
                raise ToolError(
                    "waited %ds for %s, which transaction %s still holds. "
                    "Give up on it and say so." % (timeout, resource, holder))
            _t.sleep(0.25)


def run(root: str, command: str) -> str:
    p = subprocess.run(command, shell=True, cwd=root, capture_output=True,
                       text=True, timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + "\n... (truncated)"
    return "exit %d\n%s" % (p.returncode, out.strip() or "(no output)")


DISPATCH = {
    "list_dir": lambda root, a: list_dir(root, a.get("path", ".")),
    "read_file": lambda root, a: read_file(root, a["path"]),
    "write_file": lambda root, a: write_file(root, a["path"], a.get("content", "")),
    "edit_file": lambda root, a: edit_file(root, a["path"], a["old"], a["new"]),
    "run": lambda root, a: run(root, a["command"]),
    "claim": lambda root, a: claim(root, a["resource"]),
}

# --- the schema the model sees ----------------------------------------
#
# Descriptions are written FOR A SMALL MODEL: short, imperative, and
# explicit about the thing it gets wrong. "Paths are relative to the
# folder" appears on every path argument because a 7B will otherwise
# produce absolute paths about a third of the time.
SCHEMA = [
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List files in a directory. Start here if unsure.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Relative to the working folder. Use '.' for the top."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file's contents. Read before you edit.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative to the working folder."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a file, or replace one completely.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative to the working folder."},
            "content": {"type": "string", "description": "The complete new contents."}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact piece of text in a file. The old text must appear exactly once.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Relative to the working folder."},
            "old": {"type": "string", "description": "Exact text to replace, copied from the file."},
            "new": {"type": "string", "description": "Replacement text."}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "claim",
        "description": ("Take exclusive hold of a shared resource before "
                        "using it. Waits if another agent holds it. Use "
                        "this for anything that cannot be done twice."),
        "parameters": {"type": "object", "properties": {
            "resource": {"type": "string",
                         "description": "The resource name, e.g. 'database'."}},
            "required": ["resource"]}}},
    {"type": "function", "function": {
        "name": "run",
        "description": "Run a shell command in the working folder.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command line."}},
            "required": ["command"]}}},
]


def call(root: str, name: str, args) -> tuple[str, bool]:
    """Execute one tool call. Returns (result text, is_error)."""
    if isinstance(args, str):
        # Some models hand back the arguments object as a JSON string.
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return "arguments were not valid JSON: %s" % args[:200], True
    if not isinstance(args, dict):
        return "arguments must be a JSON object", True
    fn = DISPATCH.get(name)
    if fn is None:
        return ("no such tool %r; you have: %s"
                % (name, ", ".join(DISPATCH))), True
    try:
        return fn(root, args), False
    except ToolError as e:
        return str(e), True
    except KeyError as e:
        return "missing required argument %s" % e, True
    except subprocess.TimeoutExpired:
        return "the command took longer than 120s and was stopped", True
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e), True
