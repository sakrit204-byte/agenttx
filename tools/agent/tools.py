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
