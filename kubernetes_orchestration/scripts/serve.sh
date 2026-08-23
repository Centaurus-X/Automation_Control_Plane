#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
config_file="${FLEET_CONFIG_FILE:-${FLEET_CONFIG_DIR:-$config_home/automation-fleet}/fleet.json}"

[[ -f "$config_file" ]] || "$root_dir/scripts/setup.sh"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$root_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m automation_fleet.cli --root "$root_dir" --config "$config_file" serve
