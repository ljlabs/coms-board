#!/usr/bin/env bash
# Run the coms-board dashboard in the foreground on 127.0.0.1.
# The terminal remains attached so startup errors and request logs are visible.
# Stop it with Ctrl+C.
#
#   bash scripts/serve.sh
#   COMS_PORT=8763 bash scripts/serve.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${COMS_PORT:-8765}"

cd "$ROOT"
exec python3 -m coms.server --host 127.0.0.1 --port "$PORT"
