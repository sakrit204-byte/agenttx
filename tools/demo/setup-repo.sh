#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/demo/setup-repo.sh --- put a real project in the sandbox.
#
#   bash tools/demo/setup-repo.sh [url] [dir]
#
# Run this IN THE GUEST. It clones a real repository into a folder the
# agent can work in, so the harness can be exercised at the scale it is
# actually meant for: a few hundred files, a real directory shape, and a
# task that touches dozens of them at once.
#
# A toy folder with three files proves nothing about this system. The
# copy-on-write layer, the structure view and the commit path all behave
# the same on three files; what a real checkout tests is whether they
# still behave when there are two hundred.

set -euo pipefail

URL=${1:-https://github.com/pallets/flask}
DIR=${2:-/tmp/repo}
USER_=${AGENT_USER:-agent}

[[ -c /dev/agenttx ]] || { echo "run this inside the guest" >&2; exit 1; }
command -v git >/dev/null || { echo "git is not installed here" >&2; exit 1; }

rm -rf "$DIR"
git clone --depth 1 "$URL" "$DIR" 2>&1 | tail -1

# The agent runs as its own user and must be able to write the tree.
# tools/ui/guest.py runs the agent as whoever owns the folder, so this
# chown is what decides that -- and it happens HERE, before any
# transaction exists, rather than inside one. Setting up the demo is not
# part of what you are being asked to review afterwards.
chown -R "$USER_" "$DIR"

printf '\n%s\n' "$DIR is ready:"
printf '  %s files\n'        "$(find "$DIR" -type f -not -path '*/.git/*' | wc -l)"
printf '  %s python files\n' "$(find "$DIR" -name '*.py' -not -path '*/.git/*' | wc -l)"
printf '  %s under src/\n'   "$(find "$DIR/src" -name '*.py' 2>/dev/null | wc -l)"
printf '  %s on disk\n'      "$(du -sh "$DIR" | cut -f1)"
