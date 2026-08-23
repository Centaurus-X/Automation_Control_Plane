#!/usr/bin/env bash
set -euo pipefail

# Report the container and health status without printing configuration secrets.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
docker compose ps mcp-controller-server
service_id="$(docker compose ps -q mcp-controller-server)"
test -n "$service_id"
docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$service_id"
