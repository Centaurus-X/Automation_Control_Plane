#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

if [[ "${1:-}" != "--confirm-destroy-all-data" ]]; then
  echo "Refusing to delete volumes without --confirm-destroy-all-data" >&2
  exit 64
fi

docker compose down --volumes --remove-orphans
echo "All PostgreSQL data and WAL volumes were deleted."
