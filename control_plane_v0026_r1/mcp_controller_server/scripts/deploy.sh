#!/usr/bin/env bash
set -euo pipefail

# Build and converge the versioned service definition idempotently.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
"$release_root/scripts/preflight.sh"
if [[ "${MCP_CONTROLLER_SKIP_BUILD:-0}" != "1" ]]; then
  docker compose build --pull
fi
docker compose up -d --remove-orphans
"$release_root/scripts/ready.sh"
