#!/usr/bin/env bash
set -euo pipefail

# Approve one exact plan inside the private state-volume boundary.
if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 <plan-id> <payload-sha256> <approver>" >&2
  exit 2
fi
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose exec -T mcp-controller-server \
  python -m mcp_controller_server.approval_cli approve \
  --plan-id "$1" --payload-sha256 "$2" --approver "$3"
