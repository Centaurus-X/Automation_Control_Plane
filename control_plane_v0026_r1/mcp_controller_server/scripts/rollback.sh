#!/usr/bin/env bash
set -euo pipefail

# Converge to an explicitly selected previously retained image.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
test -n "${MCP_CONTROLLER_ROLLBACK_IMAGE:-}"
MCP_CONTROLLER_IMAGE="$MCP_CONTROLLER_ROLLBACK_IMAGE" \
  docker compose up -d --no-build --remove-orphans mcp-controller-server
"$release_root/scripts/ready.sh"
