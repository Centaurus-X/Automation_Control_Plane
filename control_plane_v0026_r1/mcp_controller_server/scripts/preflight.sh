#!/usr/bin/env bash
set -euo pipefail

# Validate local release inputs without revealing secret contents.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"

command -v docker >/dev/null
docker compose version >/dev/null

python3 "$release_root/tools/check_runtime_secrets.py"
python3 "$release_root/tools/prepare_runtime_secrets.py"

docker compose config --quiet
docker run --rm \
  -v "$release_root:/release:ro" \
  -w /release \
  python:3.14.6-slim-bookworm \
  sh -c 'PYTHONPATH=/release/src python -m mcp_controller_server.main --config /release/config/controller_config.json check-config --skip-runtime-files'

echo "PASS mcp_controller_server preflight"
