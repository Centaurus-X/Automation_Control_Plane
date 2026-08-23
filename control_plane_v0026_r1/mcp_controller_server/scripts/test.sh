#!/usr/bin/env bash
set -euo pipefail

# Run the complete release validation and the live readiness proof when deployed.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$release_root/test_all.sh"
cd "$release_root"
service_id="$(docker compose ps -q mcp-controller-server 2>/dev/null || true)"
if [[ -n "$service_id" ]]; then
  "$release_root/scripts/ready.sh"
fi
