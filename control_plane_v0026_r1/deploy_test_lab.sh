#!/usr/bin/env bash

set -Eeuo pipefail
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$root_dir/scripts/control_plane_lifecycle.sh"

prepare_lab_upgrade() {
  prepare_control_plane_deployment upgrade "$root_dir" \
    "$CONTROL_PLANE_STATE_SOURCE"
  bind_postgres_upgrade_tls_sources "$CONTROL_PLANE_STATE_SOURCE"
}

deployment_mode=""
release_profile=lab_v2
purge_host=0
while (( $# > 0 )); do
  case "$1" in
    --fresh)
      [[ -z "$deployment_mode" || "$deployment_mode" == "fresh" ]] || {
        echo "--fresh and --upgrade are mutually exclusive." >&2
        exit 64
      }
      deployment_mode=fresh
      shift
      ;;
    --upgrade)
      [[ -z "$deployment_mode" || "$deployment_mode" == "upgrade" ]] || {
        echo "--fresh and --upgrade are mutually exclusive." >&2
        exit 64
      }
      deployment_mode=upgrade
      shift
      ;;
    --purge-host)
      purge_host=1
      shift
      ;;
    --profile)
      (( $# >= 2 )) || { echo "--profile requires a value" >&2; exit 64; }
      release_profile="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 {--fresh|--upgrade} [--purge-host] [--profile lab_v2]"
      echo "  --fresh       purge old test containers, volumes, images, caches and state"
      echo "  --upgrade  preserve volumes and import state from CONTROL_PLANE_STATE_SOURCE"
      echo "  --purge-host  with --fresh, purge every Docker resource on a dedicated host"
      echo "  --profile  selects the Master lab_v2 config profile (external Workers)"
      exit 0
      ;;
    *)
      echo "Unknown lab rollout option: $1" >&2
      exit 64
      ;;
  esac
done
[[ -n "$deployment_mode" ]] || {
  echo "Choose exactly one deployment mode: --fresh or --upgrade." >&2
  exit 64
}
if (( purge_host == 1 )) && [[ "$deployment_mode" != "fresh" ]]; then
  echo "--purge-host is supported only with --fresh." >&2
  exit 64
fi
[[ "$release_profile" == "lab_v2" ]] || {
  echo "The test rollout supports only --profile lab_v2." >&2
  exit 64
}
if [[ "$deployment_mode" == "upgrade" ]]; then
  [[ -n "${CONTROL_PLANE_STATE_SOURCE:-}" ]] || {
    echo "--upgrade requires CONTROL_PLANE_STATE_SOURCE." >&2
    exit 64
  }
fi

require_control_plane_tools
"$root_dir/tools/verify_checksums.sh"
export CONTROL_PLANE_PROFILE="$release_profile"
export MASTER_RELEASE_PROFILE="$release_profile"
export MASTER_AUTO_ADMIT_BROKER_BOUND_WORKERS=true
export MASTER_INITIAL_CONFIG_FILE="$root_dir/master_node/master_node/_config/application/initial_config_v9.json"
acquire_control_plane_lock
cleanup() {
  result="$?"
  if (( result != 0 )); then
    echo "Lab rollout failed in stage: ${CONTROL_PLANE_CURRENT_STAGE:-unknown}." >&2
    echo "Current container state follows." >&2
    print_control_plane_container_status >&2 || true
  fi
  release_control_plane_lock
  exit "$result"
}
trap cleanup EXIT

bind_address="${CONTROL_PLANE_BIND_ADDRESS:-$(default_lab_bind_address)}"
export MQTT_BIND_ADDRESS="${MQTT_BIND_ADDRESS:-$bind_address}"
export MQTT_TEST_HOST="${MQTT_TEST_HOST:-$MQTT_BIND_ADDRESS}"
export MQTT_TEST_BROKER_IP="${MQTT_TEST_BROKER_IP:-$MQTT_BIND_ADDRESS}"
export CONTROL_PLANE_TEST_MODE=1
export MQTT_HOST=mqtt-broker
export MQTT_PORT=8883
export MASTER_MQTT_MAX_PACKET_SIZE="${MASTER_MQTT_MAX_PACKET_SIZE:-4194304}"
export PROXY_BIND_ADDRESS="${PROXY_BIND_ADDRESS:-$bind_address}"
export PROXY_ORIGIN_ALLOWLIST="${PROXY_ORIGIN_ALLOWLIST:-https://$bind_address:8444}"
export AUTOMATION_SITE_ID="${AUTOMATION_SITE_ID:-site-main}"
export MCP_BIND_ADDRESS="${MCP_BIND_ADDRESS:-127.0.0.1}"
export MCP_HTTP_PORT="${MCP_HTTP_PORT:-8080}"
export MCP_MQTT_SOURCE_INSTANCE_ID="${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}"
export MCP_AUTH_SOURCE="${MCP_AUTH_SOURCE:-$HOME/.config/automation-control-plane/mcp_auth.json}"
export LAB_PKI_DIR="$root_dir/mqtt_broker/test-pki"
validate_proxy_deployment_config "$root_dir"
validate_control_plane_compose_files "$root_dir"

echo "Selected lab deployment mode/profile: $deployment_mode/$release_profile"
if [[ "$deployment_mode" == "fresh" ]]; then
  if (( purge_host == 1 )); then
    run_rollout_stage "dedicated Docker host purge" purge_dedicated_docker_host
  fi
  run_rollout_stage "1/11 fresh purge" \
    prepare_control_plane_deployment "$deployment_mode" "$root_dir" ""
  run_rollout_stage "2/11 port preflight" verify_lab_ports_available
  run_rollout_stage "3/11 containerized static validation" \
    "$root_dir/test_static_all.sh"
  run_rollout_stage "4/11 clean-room image staging" \
    prepare_control_plane_images "$root_dir"
else
  run_rollout_stage "1/11 containerized static validation" \
    "$root_dir/test_static_all.sh"
  run_rollout_stage "2/11 clean-room image staging" \
    prepare_control_plane_images "$root_dir"
  run_rollout_stage "3/11 upgrade deployment preparation" \
    prepare_lab_upgrade
  run_rollout_stage "4/11 port preflight" verify_lab_ports_available
fi
run_rollout_stage "5/11 MQTT deployment" "$root_dir/mqtt_broker/deploy_test.sh"
run_rollout_stage "6/11 MQTT security and persistence tests" "$root_dir/mqtt_broker/test.sh"
export MQTT_SECURITY_TESTED=1
run_rollout_stage "7/11 PostgreSQL deployment" "$root_dir/postgres_system/deploy_test.sh"
run_rollout_stage "8/11 Master deployment" "$root_dir/master_node/deploy_test.sh"
run_rollout_stage "9/11 Proxy deployment" "$root_dir/proxy_gateway/deploy_test.sh"
export MCP_CONTROLLER_SKIP_BUILD=1
run_rollout_stage "10/11 MCP Controller Server deployment" "$root_dir/mcp_controller_server/deploy_test.sh"
run_rollout_stage "11/11 Control Plane running acceptance" "$root_dir/test_running_all.sh"

echo "Control Plane 0.26.0/v0026-r1 with MCP Controller Server 0.1.0/v0001-r1 is deployed and tested on $bind_address ($deployment_mode mode)."
echo "Worker v0101 is a separate package and must be started on its Worker host."
