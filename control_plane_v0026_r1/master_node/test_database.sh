#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

container_id="$(docker compose ps -q master-node)"
[[ -n "$container_id" ]] || {
  echo "Master container is not running." >&2
  exit 69
}
docker compose exec -T master-node python -m database_transaction_probe
