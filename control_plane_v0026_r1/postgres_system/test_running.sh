#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

bash -n test_running.sh scripts/verify_role_credentials.sh \
  scripts/verify_replication.sh
docker compose config --quiet

verify_healthy() {
  local service="$1"
  local container_id=""
  local status=""
  local health=""

  container_id="$(docker compose ps -q "$service")"
  [[ -n "$container_id" ]] || {
    echo "PostgreSQL service is not running: $service" >&2
    return 69
  }
  status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
  health="$(docker inspect --format '{{.State.Health.Status}}' "$container_id")"
  [[ "$status" == "running" && "$health" == "healthy" ]] || {
    echo "PostgreSQL service is not healthy: $service ($status/$health)" >&2
    return 1
  }
}

verify_healthy postgres-primary
verify_healthy postgres-standby
"$root_dir/scripts/verify_role_credentials.sh"
"$root_dir/scripts/verify_replication.sh"

echo "PostgreSQL running-service health, role authentication and replication probes passed."
