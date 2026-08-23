#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
config_dir="${FLEET_CONFIG_DIR:-$config_home/automation-fleet}"
state_dir="${FLEET_STATE_DIR:-$state_home/automation-fleet}"
runtime_dir="$config_dir/runtime"

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 69; }
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
  echo "Python 3.12 or newer is required" >&2
  exit 69
}

install -d -m 0700 "$config_dir" "$runtime_dir" "$state_dir" "$state_dir/plans" "$state_dir/evidence"

install_if_missing() {
  local source="$1"
  local target="$2"
  if [[ ! -e "$target" ]]; then
    install -m 0600 "$source" "$target"
  fi
}

install_if_missing "$root_dir/config/fleet.example.json" "$config_dir/fleet.json"
install_if_missing "$root_dir/config/inventory.example.json" "$config_dir/inventory.example.json"
install_if_missing "$root_dir/config/release_catalog.json" "$config_dir/release_catalog.json"
install_if_missing "$root_dir/config/desired_state.example.json" "$config_dir/desired_state.example.json"
install_if_missing "$root_dir/config/policy.example.json" "$config_dir/policy.example.json"
install_if_missing "$root_dir/config/agent_registry.example.json" "$runtime_dir/agents.json"

generate_token() {
  local target="$1"
  if [[ ! -e "$target" ]]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$target"
    chmod 0600 "$target"
  fi
}

generate_token "$runtime_dir/admin.token"
generate_token "$runtime_dir/operator.token"
generate_token "$runtime_dir/approver.token"
generate_token "$runtime_dir/auditor.token"
generate_token "$runtime_dir/viewer.token"

echo "Fleet configuration: $config_dir/fleet.json"
echo "Fleet state: $state_dir"
echo "Admin token file: $runtime_dir/admin.token"
echo "Operator token file: $runtime_dir/operator.token"
echo "Approver token file: $runtime_dir/approver.token"
echo "Auditor token file: $runtime_dir/auditor.token"
echo "Viewer token file: $runtime_dir/viewer.token"
