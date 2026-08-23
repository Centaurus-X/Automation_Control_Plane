#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tail_count="${LOG_TAIL:-100}"
for package in mqtt_broker postgres_system master_node proxy_gateway mcp_controller_server; do
  echo "===== $package ====="
  (cd "$root_dir/$package" && docker compose logs --tail "$tail_count" --no-color) || true
done
