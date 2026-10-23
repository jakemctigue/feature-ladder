#!/bin/bash
# vexp-restore: context lifecycle restore on SessionStart (compact/resume). Fails open.
VEXP_BIN="C:/Users/Jake/AppData/Roaming/npm/node_modules/vexp-cli/node_modules/@vexp/core-win32-x64/bin/vexp-core.exe"
[ -x "$VEXP_BIN" ] || exit 0
"$VEXP_BIN" session-context 2>/dev/null
exit 0
