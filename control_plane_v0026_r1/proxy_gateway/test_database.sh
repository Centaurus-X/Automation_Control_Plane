#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

container_id="$(docker compose ps -q proxy-gateway)"
[[ -n "$container_id" ]] || {
  echo "Proxy container is not running." >&2
  exit 69
}
docker compose exec -T proxy-gateway python -m database_transaction_probe
docker compose exec -T proxy-gateway python -m database_security_probe
