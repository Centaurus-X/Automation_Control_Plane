#!/usr/bin/env bash

set -Eeuo pipefail
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
state_dir="${FLEET_STATE_DIR:-$state_home/automation-fleet}"
pid_file="$state_dir/fleet.pid"
instance_file="$state_dir/fleet.instance"
listen_host="${FLEET_LISTEN_HOST:-127.0.0.1}"
listen_port="${FLEET_LISTEN_PORT:-9080}"

if [[ ! -f "$pid_file" || ! -f "$instance_file" ]]; then
  echo "Automation Fleet: STOPPED"
  exit 3
fi
pid="$(<"$pid_file")"
instance_id="$(<"$instance_file")"
if [[ ! "$pid" =~ ^[1-9][0-9]*$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  echo "Automation Fleet: STALE PID FILE"
  exit 4
fi
FLEET_HEALTH_URL="http://${listen_host}:${listen_port}/api/v1/health/ready" \
FLEET_EXPECTED_PID="$pid" FLEET_EXPECTED_INSTANCE="$instance_id" \
  python3 -c 'import json,os,urllib.request; value=json.load(urllib.request.urlopen(os.environ["FLEET_HEALTH_URL"],timeout=2)); assert str(value.get("pid"))==os.environ["FLEET_EXPECTED_PID"] and value.get("instance_id")==os.environ["FLEET_EXPECTED_INSTANCE"], "service identity mismatch"; print(json.dumps(value,sort_keys=True))'
echo "Automation Fleet: RUNNING pid=$pid"
