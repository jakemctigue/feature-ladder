#!/usr/bin/env bash
# Run a Python script with whichever interpreter exists (python3 on Linux/macOS,
# often only `python` on Windows/Git Bash). stdin is passed through untouched.
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" "$@"
  fi
done
echo "feature-ladder: no python interpreter found on PATH" >&2
exit 0
