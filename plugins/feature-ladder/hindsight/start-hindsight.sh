#!/usr/bin/env bash
# Start (or restart) a local Hindsight server with the feature-ladder model
# policy (cheapest adequate model per operation). Docker is the supported path.
#
#   cp hindsight.env.example hindsight.env   # fill in HINDSIGHT_API_LLM_API_KEY
#   ./start-hindsight.sh
#
# On Windows, run this from Git Bash or WSL, or paste the docker command below
# into PowerShell with --env-file pointing at hindsight.env.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$HERE/hindsight.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "No env file at $ENV_FILE. Copy hindsight.env.example to hindsight.env and set your API key." >&2
  exit 1
fi
if grep -q "REPLACE_ME" "$ENV_FILE"; then
  echo "hindsight.env still contains REPLACE_ME - set HINDSIGHT_API_LLM_API_KEY first." >&2
  exit 1
fi

docker rm -f hindsight >/dev/null 2>&1 || true
docker run -d --pull always --name hindsight --restart unless-stopped \
  -p 8888:8888 -p 9999:9999 \
  --env-file "$ENV_FILE" \
  -v hindsight-data:/home/hindsight/.pg0 \
  ghcr.io/vectorize-io/hindsight:latest

echo "Hindsight starting: API http://localhost:8888  UI http://localhost:9999"
echo "MCP endpoint for the plugin: http://localhost:8888/mcp/  (bank via X-Bank-Id, default feature-ladder)"
echo "Verify inside Claude Code with /mcp, or: curl -s -X POST http://localhost:8888/mcp/ -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{\"jsonrpc\":\"2.0\",\"method\":\"tools/list\",\"id\":1}' | head -c 300"
