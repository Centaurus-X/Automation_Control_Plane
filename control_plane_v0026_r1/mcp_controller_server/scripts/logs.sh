#!/usr/bin/env bash
set -euo pipefail

# Follow bounded service output; application logs never contain bearer tokens.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose logs --tail "${MCP_LOG_TAIL:-200}" --follow mcp-controller-server
