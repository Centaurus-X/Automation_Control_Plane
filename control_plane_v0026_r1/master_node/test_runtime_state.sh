#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
health_url="${MASTER_HEALTH_URL:-http://127.0.0.1:9090/health/ready}"
expected_nodes="${MASTER_EXPECTED_NODE_SCOPES:-}"

arguments=(--url "$health_url")
IFS=',' read -r -a node_scopes <<< "$expected_nodes"
for scope in "${node_scopes[@]}"; do
  [[ -n "$scope" ]] && arguments+=(--expected-node-scope "$scope")
done

if [[ -n "${MASTER_EXPECTED_CROSS_SCOPES:-}" ]]; then
  IFS=',' read -r -a cross_scopes <<< "$MASTER_EXPECTED_CROSS_SCOPES"
  for scope in "${cross_scopes[@]}"; do
    [[ -n "$scope" ]] && arguments+=(--expected-cross-scope "$scope")
  done
fi

exec python3 "$root_dir/master_node/src/runtime_state_probe.py" "${arguments[@]}"
