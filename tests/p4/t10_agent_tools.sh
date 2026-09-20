#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t10_agent_tools.sh --- the agent's tools, and the mistakes a
# small model actually makes.
#
# These are not hypothetical inputs. A 7B reaches for an absolute path
# because most paths it has ever seen are absolute; it reaches for
# edit_file with a one-line `old` that matches six places; it asks to read
# a file far larger than its context. None of those are attacks and all of
# them break something, so each one gets an assertion.
#
# Tier 0: pure Python, no kernel, no model, no network.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v python3 >/dev/null || { echo "needs python3"; exit 77; }

python3 - "$REPO" <<'PY'
import os, sys, tempfile
sys.path.insert(0, os.path.join(sys.argv[1], "tools", "agent"))
import tools as T

fail = 0
def ok(m):  print("PASS:", m)
def bad(m): 
    global fail
    print("FAIL:", m); fail += 1

root = tempfile.mkdtemp()
open(os.path.join(root, "a.txt"), "w").write("hello\nworld\nhello\n")
os.makedirs(os.path.join(root, "sub"))
open(os.path.join(root, "sub", "b.txt"), "w").write("nested\n")

# --- the boundary -----------------------------------------------------
# Inside the transaction these reads would SUCCEED -- the overlay only
# covers the working directory, everything else is the real filesystem --
# so the tools have to refuse them independently of the kernel.
out, err = T.call(root, "read_file", {"path": "/etc/passwd"})
(ok if err else bad)("an absolute path outside the folder is refused")
out, err = T.call(root, "read_file", {"path": "../../etc/passwd"})
(ok if err else bad)("a .. escape is refused")
out, err = T.call(root, "write_file", {"path": "/tmp/pwned", "content": "x"})
(ok if err else bad)("writing outside the folder is refused")
if os.path.exists("/tmp/pwned"):
    bad("the refused write happened anyway"); os.unlink("/tmp/pwned")

# A path that merely LOOKS like an escape but resolves inside is fine.
out, err = T.call(root, "read_file", {"path": "sub/../a.txt"})
(ok if not err else bad)("a path that resolves back inside is allowed")

# --- edit_file must refuse an ambiguous match -------------------------
out, err = T.call(root, "edit_file",
                  {"path": "a.txt", "old": "hello", "new": "HI"})
if err and "2 times" in out:
    ok("an edit matching twice is refused, and says how many")
else:
    bad("an ambiguous edit was not refused: %r" % out[:80])
if open(os.path.join(root, "a.txt")).read().count("hello") != 2:
    bad("the refused edit modified the file anyway")
else:
    ok("the refused edit left the file untouched")

out, err = T.call(root, "edit_file",
                  {"path": "a.txt", "old": "world", "new": "WORLD"})
(ok if not err else bad)("a unique edit is applied")
(ok if "WORLD" in open(os.path.join(root, "a.txt")).read() else bad)(
    "the unique edit reached the file")

out, err = T.call(root, "edit_file",
                  {"path": "a.txt", "old": "nope", "new": "x"})
(ok if err else bad)("an edit whose text is absent is refused")

# A 7B reaches for old:"" when it wants to PREPEND, and str.count("") is
# len+1, so the first version told it "that text appears 2073 times in a
# 2072-byte file" -- which is true, useless, and unrecoverable.
out, err = T.call(root, "edit_file",
                  {"path": "a.txt", "old": "", "new": "header\n"})
if err and "must not be empty" in out:
    ok("an empty search string is refused, and says what to do instead")
else:
    bad("empty search string handled badly: %r" % out[:90])

# --- bad arguments must come back as a message, not a crash -----------
out, err = T.call(root, "read_file", {})
(ok if err and "path" in out else bad)("a missing argument is reported")
out, err = T.call(root, "nonexistent_tool", {})
(ok if err and "no such tool" in out else bad)("an unknown tool is reported")
out, err = T.call(root, "read_file", "{\"path\": \"a.txt\"}")
(ok if not err else bad)("arguments arriving as a JSON string still work")
out, err = T.call(root, "read_file", "not json at all")
(ok if err else bad)("unparseable arguments are reported, not raised")

# --- truncation -------------------------------------------------------
big = os.path.join(root, "big.txt")
open(big, "w").write("x" * (T.MAX_READ + 5000))
out, err = T.call(root, "read_file", {"path": "big.txt"})
if not err and len(out) <= T.MAX_READ + 200 and "truncated" in out:
    ok("an oversized read is truncated and says so")
else:
    bad("oversized read not truncated (%d bytes)" % len(out))

# --- run --------------------------------------------------------------
out, err = T.call(root, "run", {"command": "echo hi; echo oops >&2; exit 3"})
if "exit 3" in out and "hi" in out and "oops" in out:
    ok("run reports the exit code and both output streams")
else:
    bad("run lost the exit code or a stream: %r" % out[:120])

# run's cwd must be the folder, or every relative path the model uses is
# resolved against whatever directory the harness happened to start in.
out, err = T.call(root, "run", {"command": "pwd"})
(ok if os.path.realpath(root) in out else bad)("run executes in the folder")

sys.exit(fail)
PY
