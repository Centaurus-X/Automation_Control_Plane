#!/usr/bin/env bash
set -euo pipefail

# Wait for Docker and the MQTT-backed readiness endpoint.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$release_root"
service_id="$(docker compose ps -q mcp-controller-server)"
test -n "$service_id"

for attempt in $(seq 1 60)
do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service_id")"
  if [[ "$status" == "healthy" ]]; then
    echo "PASS mcp_controller_server ready"
    exit 0
  fi
  if [[ "$status" == "unhealthy" || "$status" == "exited" || "$status" == "dead" ]]; then
    docker compose logs --tail 100 mcp-controller-server >&2
    exit 1
  fi
  sleep 2
done

docker compose logs --tail 100 mcp-controller-server >&2
echo "mcp_controller_server readiness timeout" >&2
exit 1
