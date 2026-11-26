#!/usr/bin/env bash
# PreToolUse(Bash) launcher for the outside-repo audit hook.
#
# The hook runs before EVERY Bash call. When the Rust build of it exists
# (hooks/bin/guard_irreversible, made by build_guard.sh) it runs that and no
# Python starts at all; the binary itself falls back to guard_irreversible.py
# whenever the Python reference has changed since it was built. Without the
# binary this is exactly the old hook: py.sh runs guard_irreversible.py.
# Only shell builtins here: a fork costs real time under Git Bash.
case $0 in */*) here=${0%/*} ;; *) here=. ;; esac
bin="$here/../bin/guard_irreversible"
if [ -x "$bin" ] || [ -x "$bin.exe" ]; then
  exec "$bin"
fi
. "$here/py.sh" "$here/guard_irreversible.py"
