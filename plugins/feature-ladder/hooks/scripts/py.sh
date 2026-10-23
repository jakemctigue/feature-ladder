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
    # The audit hook is a log, not a gate: without Python the command still
    # runs, it just is not recorded. Say so once so the report can mention it.
    echo "feature-ladder: the outside-repo audit log skipped this command (no python on PATH)" >&2
    ;;
esac
exit 0
