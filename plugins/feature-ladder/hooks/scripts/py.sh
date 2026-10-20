#!/usr/bin/env bash
# Run a Python script with whichever interpreter exists (python3 on Linux/macOS,
# often only `python` on Windows/Git Bash). stdin is passed through untouched.
# A candidate must actually run (the Windows Store "python3" alias is a stub
# that exits 9009 and opens the Store; it is skipped).
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "pass" >/dev/null 2>&1; then
    exec "$py" "$@"
  fi
done
echo "feature-ladder: no working python interpreter found on PATH" >&2
case "$*" in
  *guard_irreversible.py*)
    # The guard must fail closed while a ladder run is active: deny rather than
    # run unguarded. Outside a run, do nothing.
    # The hook payload's "cwd" is where the command would run; fall back to $PWD.
    payload=""; while IFS= read -r line || [ -n "$line" ]; do payload="$payload$line"; done
    d=""
    if [[ "$payload" =~ \"cwd\":\ *\"([^\"]*)\" ]]; then d="${BASH_REMATCH[1]}"; fi
    d="${d:-$PWD}"
    d="${d//\\\\/\\}"   # JSON-escaped backslashes (Windows paths)
    while [ -n "$d" ] && [ "$d" != "/" ]; do
      if [ -f "$d/.feature-ladder/run.json" ]; then
        content=""; while IFS= read -r line || [ -n "$line" ]; do content="$content$line"; done < "$d/.feature-ladder/run.json"
        if [[ "$content" =~ \"active\":\ *true ]]; then
          printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"feature-ladder: python is not on PATH, so the outside-repo guard cannot run during this ladder run. Install Python or fix PATH, then retry."}}'
        fi
        break
      fi
      [ -e "$d/.git" ] && break
      d="${d%/*}"
    done
    ;;
esac
exit 0
