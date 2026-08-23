#!/usr/bin/env bash
set -euo pipefail

# Inspect one plan state without returning its stored mutation payload.
if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 <plan-id>" >&2
  exit 2
fi
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose exec -T mcp-controller-server \
  python -m mcp_controller_server.approval_cli status --plan-id "$1"
