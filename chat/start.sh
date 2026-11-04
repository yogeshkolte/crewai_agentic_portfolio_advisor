#!/usr/bin/env bash
# Start the stock chat: the Python agent (port 8000) and the chat page (port 3000).
# Both stay up until you press Ctrl+C; if either stops, the other is stopped too.
#
#   ./chat/start.sh          # from the project root
#   open http://127.0.0.1:3000
#
# Both listen on 127.0.0.1 only, so nothing is reachable from other machines.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/chat/web"
cd "$ROOT"

# First run: install the page's packages, create its settings file and build it.
if [ ! -d "$WEB/node_modules" ]; then
  (cd "$WEB" && npm install)
fi
if [ ! -f "$WEB/.env.local" ]; then
  cp "$WEB/local-settings.example" "$WEB/.env.local"
fi
if [ ! -d "$WEB/.next" ] || [ -n "$(find "$WEB/app" -newer "$WEB/.next/BUILD_ID" -type f 2>/dev/null)" ]; then
  (cd "$WEB" && npm run build)
fi

uv run uvicorn chat.server:app --host 127.0.0.1 --port 8000 &
AGENT=$!
(cd "$WEB" && npm run start) &
PAGE=$!

# On exit, stop everything this script started (npm and uv each start child processes).
trap 'trap - INT TERM EXIT; kill 0 2>/dev/null' INT TERM EXIT
echo "Stock chat: http://127.0.0.1:3000  (agent on http://127.0.0.1:8000, Ctrl+C to stop)"
# Works with macOS's bash 3.2 (no `wait -n`): stop when either process exits.
while kill -0 $AGENT 2>/dev/null && kill -0 $PAGE 2>/dev/null; do sleep 2; done
