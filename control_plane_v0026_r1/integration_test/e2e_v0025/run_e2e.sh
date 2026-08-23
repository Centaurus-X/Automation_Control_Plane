#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
release_root="$(cd "$root_dir/../.." && pwd)"
result_dir="${E2E_RESULT_DIR:-$root_dir/results}"

map_validation_path() {
  local variable_name="$1"
  local output_only="${2:-0}"
  local supplied="${!variable_name:-}"
  local canonical=""
  local mapped=""
  [[ -n "$supplied" ]] || return 0
  canonical="$(realpath -m -- "$supplied")" || {
    echo "BLOCKED: cannot resolve $variable_name." >&2
    return 64
  }
  case "$canonical" in
    "$validation_result_root")
      mapped=/results
      ;;
    "$validation_result_root"/*)
      mapped="/results/${canonical#"$validation_result_root"/}"
      ;;
    "$validation_release_root")
      if [[ "$output_only" == "1" ]]; then
        echo "BLOCKED: $variable_name must be inside E2E_RESULT_DIR." >&2
        return 64
      fi
      mapped=/release
      ;;
    "$validation_release_root"/*)
      if [[ "$output_only" == "1" ]]; then
        echo "BLOCKED: $variable_name must be inside E2E_RESULT_DIR." >&2
        return 64
      fi
      mapped="/release/${canonical#"$validation_release_root"/}"
      ;;
    *)
      echo "BLOCKED: $variable_name must be inside the release or E2E_RESULT_DIR." >&2
      return 64
      ;;
  esac
  printf -v "$variable_name" '%s' "$mapped"
  export "$variable_name"
}

if [[ "${CONTROL_PLANE_VALIDATION_ACTIVE:-}" != "/release" ]]; then
  command -v realpath >/dev/null 2>&1 || {
    echo "BLOCKED: realpath from coreutils is required for safe E2E path mapping." >&2
    exit 69
  }
  install -d -m 0700 "$result_dir"
  validation_release_root="$(cd "$release_root" && pwd -P)"
  validation_result_root="$(cd "$result_dir" && pwd -P)"
  if [[ "$validation_release_root" == "$validation_result_root" \
      || "$validation_release_root" == "$validation_result_root"/* ]]; then
    echo "BLOCKED: E2E_RESULT_DIR must not contain the release root." >&2
    exit 64
  fi
  for variable_name in \
      E2E_CA_FILE \
      E2E_PROXY_LOG_SOURCE \
      E2E_PROTOCOL_LOG_SOURCE \
      E2E_SECRET_LOG_SOURCE \
      E2E_BROKER_INTROSPECTION_PROOF \
      E2E_BROKER_PROOF_PUBLIC_KEY \
      E2E_BROKER_RELEASE_PROOF \
      E2E_FN02_RUNTIME_CONFIG \
      E2E_WORKER_LAUNCHER_COMMAND_FILE; do
    map_validation_path "$variable_name"
  done
  for variable_name in E2E_RESULT_FILE E2E_LOG_FILE E2E_RECOVERY_FILE; do
    map_validation_path "$variable_name" 1
  done
  export CONTROL_PLANE_VALIDATION_RESULT_DIR="$validation_result_root"
  exec "$release_root/tools/run_in_validation_container.sh" \
    /release/integration_test/e2e_v0025/run_e2e.sh "$@"
fi
profile="${E2E_PROFILE:-core}"

if [[ "${E2E_ENABLE_FN02_SIMULATION:-0}" == "1" ]]; then
  profile="full-lab"
fi
case "$profile" in
  core|full-lab) ;;
  *)
    echo "E2E_PROFILE must be core or full-lab." >&2
    exit 64
    ;;
esac

install -d -m 0700 "$result_dir"

arguments=(
  --profile "$profile"
  --https-url "${E2E_HTTPS_URL:-https://192.168.0.82:8444}"
  --wss-url "${E2E_WSS_URL:-wss://192.168.0.82:8443}"
  --origin "${E2E_ORIGIN:-https://192.168.0.82:8444}"
  --ca-file "${E2E_CA_FILE:-$root_dir/../../proxy_gateway/test-pki/wss/ca.crt}"
  --username "${E2E_USERNAME:-admin}"
  --password-env "${E2E_PASSWORD_ENV:-AUTOMATION_ADMIN_PASSWORD}"
  --bearer-token-env "${E2E_BEARER_TOKEN_ENV:-E2E_BEARER_TOKEN}"
  --site-id "${E2E_SITE_ID:-site-main}"
  --node-id "${E2E_NODE_ID:-fn-01}"
  --actuator-id "${E2E_ACTUATOR_ID:-1}"
  --sensor-id "${E2E_SENSOR_ID:-1}"
  --approved-actuator-host "${E2E_APPROVED_ACTUATOR_HOST:-192.168.0.3}"
  --approved-actuator-port "${E2E_APPROVED_ACTUATOR_PORT:-5020}"
  --http-timeout "${E2E_HTTP_TIMEOUT:-10}"
  --command-timeout "${E2E_COMMAND_TIMEOUT:-15}"
  --event-timeout "${E2E_EVENT_TIMEOUT:-45}"
  --operation-timeout "${E2E_OPERATION_TIMEOUT:-90}"
  --parallel-clients "${E2E_PARALLEL_CLIENTS:-128}"
  --result-file "${E2E_RESULT_FILE:-$result_dir/e2e-v0025-result.json}"
  --log-file "${E2E_LOG_FILE:-$result_dir/e2e-v0025.jsonl}"
  --recovery-file "${E2E_RECOVERY_FILE:-$result_dir/e2e-v0025-recovery.json}"
)

if [[ -n "${E2E_CONTROLLER_UUID:-}" ]]; then
  arguments+=(--controller-uuid "$E2E_CONTROLLER_UUID")
fi
if [[ -n "${E2E_PROXY_LOG_SOURCE:-}" ]]; then
  arguments+=(--proxy-log-source "$E2E_PROXY_LOG_SOURCE")
fi
if [[ -n "${E2E_PROTOCOL_LOG_SOURCE:-}" ]]; then
  arguments+=(--protocol-log-source "$E2E_PROTOCOL_LOG_SOURCE")
fi
if [[ -n "${E2E_SECRET_LOG_SOURCE:-}" ]]; then
  arguments+=(--secret-log-source "$E2E_SECRET_LOG_SOURCE")
fi

if [[ "${E2E_CONFIRM_MUTATIONS:-0}" == "1" ]]; then
  arguments+=(--confirm-mutations)
fi
if [[ -n "${E2E_LAST_EVENT_ID:-}" ]]; then
  arguments+=(--last-event-id "$E2E_LAST_EVENT_ID")
fi
if [[ -n "${E2E_RUN_ID:-}" ]]; then
  arguments+=(--run-id "$E2E_RUN_ID")
fi
if [[ "${E2E_VERBOSE:-0}" == "1" ]]; then
  arguments+=(--verbose)
fi
if [[ -n "${E2E_BROKER_INTROSPECTION_PROOF:-}" ]]; then
  arguments+=(--broker-introspection-proof "$E2E_BROKER_INTROSPECTION_PROOF")
fi
if [[ -n "${E2E_BROKER_PROOF_PUBLIC_KEY:-}" ]]; then
  arguments+=(--broker-proof-public-key "$E2E_BROKER_PROOF_PUBLIC_KEY")
fi
if [[ -n "${E2E_BROKER_PROOF_KEY_SHA256:-}" ]]; then
  arguments+=(--broker-proof-key-sha256 "$E2E_BROKER_PROOF_KEY_SHA256")
fi
if [[ -n "${E2E_BROKER_RELEASE_PROOF:-}" ]]; then
  arguments+=(--broker-release-proof "$E2E_BROKER_RELEASE_PROOF")
fi
if [[ -n "${E2E_FN02_HEARTBEAT_STALE_S:-}" ]]; then
  arguments+=(--fn02-heartbeat-stale-s "$E2E_FN02_HEARTBEAT_STALE_S")
fi
if [[ -n "${E2E_FN02_RUNTIME_CONFIG:-}" ]]; then
  arguments+=(--fn02-runtime-config "$E2E_FN02_RUNTIME_CONFIG")
fi
if [[ -n "${E2E_WORKER_LAUNCHER_COMMAND_FILE:-}" ]]; then
  arguments+=(--worker-launcher-command-file "$E2E_WORKER_LAUNCHER_COMMAND_FILE")
fi

exec python3 "$root_dir/control_plane_wss_e2e_test.py" "${arguments[@]}" "$@"
