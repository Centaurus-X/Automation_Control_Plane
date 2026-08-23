#!/usr/bin/env bash

set -Eeuo pipefail
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
state_dir="${FLEET_STATE_DIR:-$state_home/automation-fleet}"
log_file="$state_dir/fleet.log"
[[ -f "$log_file" ]] || { echo "Fleet log is not available: $log_file" >&2; exit 1; }
exec tail -n 200 -f "$log_file"
