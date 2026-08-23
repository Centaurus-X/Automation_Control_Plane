#!/usr/bin/env bash
set -euo pipefail

# Create an online SQLite snapshot and complete-line audit copy in the state volume.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
backup_name="backup-$(date -u +%Y%m%dT%H%M%SZ)"
docker compose exec -T mcp-controller-server \
  python -m mcp_controller_server.maintenance backup \
  --directory "/app/state/backups/$backup_name"
