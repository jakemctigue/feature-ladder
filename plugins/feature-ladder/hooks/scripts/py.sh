#!/usr/bin/env bash
# Run a Python script with whichever interpreter exists (python3 on Linux/macOS,
# often only `python` on Windows/Git Bash). stdin is passed through untouched.
#
# Every hook goes through here, before every Bash call among others, so Python
# is started ONCE: a candidate outside the Windows Store folder is a real
# interpreter and is exec'd directly. Only a candidate under .../WindowsApps/
# can be the Store stub (it exits 9009 and opens the Store), so only those are
# probed, and the one that works is remembered in a temp file.
export PYTHONUTF8=1   # hook output is UTF-8 whatever the console code page

store=""
for py in python3 python py; do
  p=$(command -v "$py" 2>/dev/null) || continue
  case "$p" in
    *WindowsApps*) store="$store $py" ;;
    *) exec "$py" "$@" ;;
  esac
done

if [ -n "$store" ]; then
  cache="${TMPDIR:-/tmp}/feature-ladder-py-${USER:-${USERNAME:-user}}"
  if [ -r "$cache" ]; then
    read -r py < "$cache"
    case " $store " in
      *" $py "*) exec "$py" "$@" ;;
    esac
  fi
  for py in $store; do
    if "$py" -c "pass" >/dev/null 2>&1; then
      printf '%s\n' "$py" > "$cache" 2>/dev/null
      exec "$py" "$@"
    fi
  done
fi

echo "feature-ladder: no working python interpreter found on PATH" >&2
case "$*" in
  *guard_irreversible.py*)
    # The audit hook is a log, not a gate: without Python the command still
    # runs, it just is not recorded. Say so once so the report can mention it.
    echo "feature-ladder: the outside-repo audit log skipped this command (no python on PATH)" >&2
    ;;
esac
exit 0
