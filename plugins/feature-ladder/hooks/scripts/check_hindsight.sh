#!/usr/bin/env bash
# SessionStart hook: tell the session whether the Hindsight MCP endpoint answers,
# so agents know up front whether memory is live. Never fails the session.
set -u
URL="${HINDSIGHT_MCP_URL:-https://api.hindsight.vectorize.io/mcp/}"
BANK="${HINDSIGHT_BANK_ID:-feature-ladder}"
AUTH=()
if [ -n "${HINDSIGHT_API_KEY:-}" ]; then AUTH=(-H "Authorization: Bearer ${HINDSIGHT_API_KEY}"); fi

if ! command -v curl >/dev/null 2>&1; then
  echo "feature-ladder: curl not found; skipping Hindsight reachability check."
  exit 0
fi

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 -X POST "$URL" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "X-Bank-Id: $BANK" "${AUTH[@]}" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}' 2>/dev/null || true)
code="${code:-000}"; [ "$code" = "000000" ] && code=000

case "$code" in
  200|202) echo "feature-ladder: Hindsight memory reachable at $URL (bank: $BANK). Agents will recall before work and retain on finish." ;;
  401|403) echo "feature-ladder: Hindsight at $URL rejected auth (HTTP $code). Set HINDSIGHT_API_KEY. Agents will run without memory until fixed." ;;
  000)     echo "feature-ladder: Hindsight not reachable at $URL. Start it (docker run ... ghcr.io/vectorize-io/hindsight:latest) or set HINDSIGHT_MCP_URL. Agents will run without memory." ;;
  *)       echo "feature-ladder: Hindsight at $URL answered HTTP $code; check the endpoint path ends with /mcp/ or /mcp/<bank>/." ;;
esac
exit 0
