#!/usr/bin/env bash

set -Eeuo pipefail
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
state_dir="${FLEET_STATE_DIR:-$state_home/automation-fleet}"
pid_file="$state_dir/fleet.pid"
instance_file="$state_dir/fleet.instance"
listen_host="${FLEET_LISTEN_HOST:-127.0.0.1}"
listen_port="${FLEET_LISTEN_PORT:-9080}"

if [[ ! -f "$pid_file" ]]; then
  echo "Automation Fleet is already stopped."
  exit 0
fi

pid="$(<"$pid_file")"
[[ "$pid" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid PID file" >&2; exit 1; }
if ! kill -0 "$pid" 2>/dev/null; then
  rm -f -- "$pid_file" "$instance_file"
  echo "Automation Fleet is already stopped."
  exit 0
fi

[[ -f "$instance_file" ]] || { echo "Missing service identity; refusing to signal PID" >&2; exit 1; }
instance_id="$(<"$instance_file")"
FLEET_HEALTH_URL="http://${listen_host}:${listen_port}/api/v1/health/ready" \
FLEET_EXPECTED_PID="$pid" FLEET_EXPECTED_INSTANCE="$instance_id" \
  python3 -c 'import json,os,urllib.request; value=json.load(urllib.request.urlopen(os.environ["FLEET_HEALTH_URL"],timeout=2)); assert str(value.get("pid"))==os.environ["FLEET_EXPECTED_PID"] and value.get("instance_id")==os.environ["FLEET_EXPECTED_INSTANCE"], "service identity mismatch"' || {
    echo "Service identity could not be verified; refusing to signal PID" >&2
    exit 1
  }

kill -TERM "$pid"
for _ in $(seq 1 30); do
  ! kill -0 "$pid" 2>/dev/null && break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  kill -KILL "$pid"
  echo "Automation Fleet required SIGKILL after the graceful timeout." >&2
fi
rm -f -- "$pid_file" "$instance_file"
echo "Automation Fleet stopped. Persistent state was preserved."
