#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
config_file="${FLEET_CONFIG_FILE:-${FLEET_CONFIG_DIR:-$config_home/automation-fleet}/fleet.json}"
state_dir="${FLEET_STATE_DIR:-$state_home/automation-fleet}"
pid_file="$state_dir/fleet.pid"
instance_file="$state_dir/fleet.instance"
log_file="$state_dir/fleet.log"
listen_host="${FLEET_LISTEN_HOST:-127.0.0.1}"
listen_port="${FLEET_LISTEN_PORT:-9080}"

[[ -f "$config_file" ]] || "$root_dir/scripts/setup.sh"
install -d -m 0700 "$state_dir"

if [[ -f "$pid_file" ]]; then
  "$root_dir/scripts/stop.sh"
fi

export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$root_dir/src${PYTHONPATH:+:$PYTHONPATH}"
instance_id="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export FLEET_INSTANCE_ID="$instance_id"

launcher=(python3 -m automation_fleet.cli \
  --root "$root_dir" --config "$config_file" serve)
if command -v setsid >/dev/null 2>&1; then
  nohup setsid "${launcher[@]}" >>"$log_file" 2>&1 </dev/null &
else
  nohup "${launcher[@]}" >>"$log_file" 2>&1 </dev/null &
fi
launcher_pid=$!
chmod 0600 "$log_file"

ready=0
for _ in $(seq 1 30); do
  if ! kill -0 "$launcher_pid" 2>/dev/null; then
    echo "Automation Fleet exited during startup. See $log_file" >&2
    exit 1
  fi
  service_pid="$(FLEET_HEALTH_URL="http://${listen_host}:${listen_port}/api/v1/health/ready" \
      FLEET_EXPECTED_INSTANCE="$instance_id" \
      python3 -c 'import json,os,urllib.request; value=json.load(urllib.request.urlopen(os.environ["FLEET_HEALTH_URL"],timeout=1)); print(value["pid"] if value.get("instance_id")==os.environ["FLEET_EXPECTED_INSTANCE"] else "")' 2>/dev/null || true)"
  if [[ "$service_pid" =~ ^[1-9][0-9]*$ ]]; then
    ready=1
    break
  fi
  sleep 1
done
[[ "$ready" == "1" ]] || {
  kill -TERM "$launcher_pid" 2>/dev/null || true
  echo "Automation Fleet did not become ready. See $log_file" >&2
  exit 1
}
printf '%s\n' "$service_pid" >"$pid_file"
printf '%s\n' "$instance_id" >"$instance_file"
chmod 0600 "$pid_file" "$instance_file"

echo "Automation Fleet started: http://${listen_host}:${listen_port}"
echo "Log: $log_file"
