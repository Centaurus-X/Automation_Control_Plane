#!/usr/bin/env bash
set -euo pipefail

# Restart the service and re-prove MQTT-backed readiness.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose restart mcp-controller-server
"$release_root/scripts/ready.sh"
