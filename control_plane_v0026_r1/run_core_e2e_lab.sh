#!/usr/bin/env bash

# Terminal-safe, real-Worker Core E2E runner for the active lab profile.

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
current_e2e_file="$root_dir/integration_test/CURRENT_E2E"
test -s "$current_e2e_file" || {
  echo "BLOCKED: active E2E selector is missing: $current_e2e_file" >&2
  exit 66
}
active_e2e_name="$(<"$current_e2e_file")"
[[ "$active_e2e_name" =~ ^e2e_v[0-9]{4,}$ ]] || {
  echo "BLOCKED: invalid active E2E selector: $active_e2e_name" >&2
  exit 65
}
active_e2e_dir="$root_dir/integration_test/$active_e2e_name"
active_e2e_serial="${active_e2e_name#e2e_}"
result_dir="${E2E_RESULT_DIR:-$active_e2e_dir/results}"
result_file="$result_dir/e2e-${active_e2e_serial}-result.json"
node_id="${E2E_NODE_ID:-fn-01}"
worker_timeout_s="${E2E_WORKER_TIMEOUT_S:-120}"
password_env="AUTOMATION_ADMIN_PASSWORD"
bearer_env="${E2E_BEARER_TOKEN_ENV:-E2E_BEARER_TOKEN}"

[[ "$node_id" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || {
  echo "BLOCKED: E2E_NODE_ID is invalid." >&2
  exit 64
}
[[ "$worker_timeout_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "BLOCKED: E2E_WORKER_TIMEOUT_S must be a positive integer." >&2
  exit 64
}
[[ "$bearer_env" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
  echo "BLOCKED: E2E_BEARER_TOKEN_ENV is not a valid variable name." >&2
  exit 64
}
command -v docker >/dev/null 2>&1 || {
  echo "BLOCKED: docker is required." >&2
  exit 69
}
command -v python3 >/dev/null 2>&1 || {
  echo "BLOCKED: python3 is required for the final report check." >&2
  exit 69
}

install -d -m 0700 "$result_dir"
: >"$result_dir/proxy-live.log"
: >"$result_dir/protocol-live.log"
chmod 0600 "$result_dir/proxy-live.log" "$result_dir/protocol-live.log"

log_pids=()
cleanup() {
  local pid=""
  for pid in "${log_pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${log_pids[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  unset AUTOMATION_ADMIN_PASSWORD
}
trap cleanup EXIT INT TERM

(
  cd "$root_dir/proxy_gateway"
  exec docker compose logs --no-color --tail 200 --follow proxy-gateway
) >>"$result_dir/proxy-live.log" 2>&1 &
log_pids+=("$!")
(
  cd "$root_dir/master_node"
  exec docker compose logs --no-color --tail 200 --follow master-node
) >>"$result_dir/protocol-live.log" 2>&1 &
log_pids+=("$!")
(
  cd "$root_dir/mqtt_broker"
  exec docker compose logs --no-color --tail 200 --follow mqtt-broker
) >>"$result_dir/protocol-live.log" 2>&1 &
log_pids+=("$!")

sleep 3
[[ -s "$result_dir/proxy-live.log" ]] || {
  echo "BLOCKED: Proxy live log is empty." >&2
  exit 1
}
[[ -s "$result_dir/protocol-live.log" ]] || {
  echo "BLOCKED: protocol live log is empty." >&2
  exit 1
}

"$root_dir/test_worker_auto_admission.sh" "$node_id" "$worker_timeout_s"

if [[ -z "${!bearer_env:-}" && -z "${AUTOMATION_ADMIN_PASSWORD:-}" ]]; then
  [[ -t 0 ]] || {
    echo "BLOCKED: set AUTOMATION_ADMIN_PASSWORD or the configured bearer variable." >&2
    exit 64
  }
  read -r -s -p 'Admin-Passwort: ' AUTOMATION_ADMIN_PASSWORD
  echo
  export AUTOMATION_ADMIN_PASSWORD
fi

# The validation runner deliberately forwards only its allowlisted E2E
# namespace.  Normalize a caller-selected bearer variable before entering the
# container so a valid host credential cannot disappear at that boundary.
if [[ -n "${!bearer_env:-}" ]]; then
  export E2E_BEARER_TOKEN="${!bearer_env}"
  export E2E_BEARER_TOKEN_ENV=E2E_BEARER_TOKEN
fi

export E2E_RESULT_DIR="$result_dir"
export E2E_PROXY_LOG_SOURCE="$result_dir/proxy-live.log"
export E2E_PROTOCOL_LOG_SOURCE="$result_dir/protocol-live.log"
export E2E_USERNAME="${E2E_USERNAME:-admin}"
export E2E_PASSWORD_ENV="$password_env"
export E2E_HTTPS_URL="${E2E_HTTPS_URL:-https://192.168.0.82:8444}"
export E2E_WSS_URL="${E2E_WSS_URL:-wss://192.168.0.82:8443}"
export E2E_ORIGIN="${E2E_ORIGIN:-https://192.168.0.82:8444}"
export E2E_APPROVED_ACTUATOR_HOST="${E2E_APPROVED_ACTUATOR_HOST:-192.168.0.3}"
export E2E_APPROVED_ACTUATOR_PORT="${E2E_APPROVED_ACTUATOR_PORT:-5020}"
export E2E_PROFILE=core
export E2E_CONFIRM_MUTATIONS=1

set +e
"$root_dir/rollout.sh" test-e2e
e2e_rc=$?
set -e
if (( e2e_rc != 0 )); then
  echo "E2E $active_e2e_serial failed with exit code $e2e_rc." >&2
  echo "Result: $result_file" >&2
  exit "$e2e_rc"
fi

python3 "$root_dir/tools/verify_core_e2e_result.py" \
  --result "$result_file" \
  --version-file "$active_e2e_dir/VERSION"
