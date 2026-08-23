#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_home="$(mktemp -d)"
cleanup() {
  FLEET_STATE_DIR="$test_home/state" \
  FLEET_CONFIG_DIR="$test_home/config" \
  FLEET_LISTEN_PORT="${test_port:-19080}" \
    "$root_dir/scripts/stop.sh" >/dev/null 2>&1 || true
  rm -rf -- "$test_home"
}
trap cleanup EXIT

test_port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
export FLEET_STATE_DIR="$test_home/state"
export FLEET_CONFIG_DIR="$test_home/config"
export FLEET_LISTEN_HOST="127.0.0.1"
export FLEET_LISTEN_PORT="$test_port"

"$root_dir/scripts/setup.sh" >/dev/null
[[ "$(stat -c '%a' "$test_home/config/runtime/admin.token")" == "600" ]]
for role in operator approver auditor viewer; do
  [[ "$(stat -c '%a' "$test_home/config/runtime/$role.token")" == "600" ]]
done
[[ "$(stat -c '%a' "$test_home/config/runtime/agents.json")" == "600" ]]
"$root_dir/scripts/start.sh" >/dev/null
"$root_dir/scripts/status.sh" >/dev/null
python3 -c 'import json,os,urllib.request; value=json.load(urllib.request.urlopen("http://127.0.0.1:"+os.environ["FLEET_LISTEN_PORT"]+"/api/v1/health/ready",timeout=2)); assert value["status"]=="ready"'
"$root_dir/scripts/stop.sh" >/dev/null
[[ ! -e "$test_home/state/fleet.pid" ]]
echo "PASS standalone service setup, start, readiness, status and stop"
