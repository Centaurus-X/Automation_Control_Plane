#!/usr/bin/env bash
set -euo pipefail

# Stop the service while preserving its durable state volume.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose stop mcp-controller-server
