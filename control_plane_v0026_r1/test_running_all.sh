#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$root_dir/scripts/control_plane_lifecycle.sh"
require_control_plane_tools

expected_control_plane_revision="${CONTROL_PLANE_EXPECTED_REVISION:-v0026-r1}"
[[ "$expected_control_plane_revision" =~ ^v[0-9]{4,}-r[1-9][0-9]*$ ]] || {
  echo "Invalid CONTROL_PLANE_EXPECTED_REVISION: $expected_control_plane_revision" >&2
  exit 64
}

verify_running_image() {
  local package="$1"
  local service="$2"
  local expected_image="$3"
  local expected_release="${4:-}"
  local expected_revision="${5:-}"
  local container_id=""
  local actual_image=""
  local actual_release=""
  local actual_revision=""
  container_id="$(cd "$root_dir/$package" && docker compose ps -q "$service")"
  if [[ -z "$container_id" ]]; then
    echo "$package/$service is missing; run './rollout.sh test' first." >&2
    return 69
  fi
  actual_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
  [[ "$actual_image" == "$expected_image" ]] || {
    echo "$package/$service is running a stale image." >&2
    echo "Expected: $expected_image" >&2
    echo "Actual:   $actual_image" >&2
    echo "The requested release was not deployed completely." >&2
    return 70
  }
  if [[ -n "$expected_release" ]]; then
    actual_release="$(docker inspect --format \
      '{{with .Config.Labels}}{{index . "org.opencontainers.image.version"}}{{end}}' \
      "$container_id")"
    [[ "$actual_release" == "$expected_release" ]] || {
      echo "$package/$service has the wrong release label." >&2
      echo "Expected release: $expected_release" >&2
      echo "Actual release:   ${actual_release:-missing}" >&2
      return 70
    }
  fi
  if [[ -n "$expected_revision" ]]; then
    actual_revision="$(docker inspect --format \
      '{{with .Config.Labels}}{{index . "org.opencontainers.image.revision"}}{{end}}' \
      "$container_id")"
    [[ "$actual_revision" == "$expected_revision" ]] || {
      echo "$package/$service has the wrong release revision." >&2
      echo "Expected revision: $expected_revision" >&2
      echo "Actual revision:   ${actual_revision:-missing}" >&2
      return 70
    }
  fi
  echo "Verified running image: $package/$service -> $actual_image"
}

wait_for_health() {
  local package="$1"
  local service="$2"
  local container_id=""
  local health=""
  local attempts="${CONTROL_PLANE_HEALTH_ATTEMPTS:-45}"
  for _attempt in $(seq 1 "$attempts"); do
    container_id="$(cd "$root_dir/$package" && docker compose ps -q "$service")"
    if [[ -z "$container_id" ]]; then
      echo "$package/$service is missing; run './rollout.sh test' first." >&2
      return 69
    fi
    health="$(docker inspect --format '{{.State.Health.Status}}' \
      "$container_id" 2>/dev/null || true)"
    [[ "$health" == "healthy" ]] && return 0
    sleep 2
  done
  echo "$package/$service did not become healthy (last state: ${health:-missing})." >&2
  if [[ "$package/$service" == "master_node/master-node" ]]; then
    echo "Master readiness body follows (HTTP 503 is diagnostic here)." >&2
    curl -sS http://127.0.0.1:9090/health/ready >&2 || true
    echo >&2
    echo "Master log tail follows." >&2
    (cd "$root_dir/master_node" && \
      docker compose logs --tail=200 --no-color master-node) >&2 || true
  fi
  return 1
}

verify_running_image mqtt_broker mqtt-broker \
  "${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
verify_running_image postgres_system postgres-primary \
  "${POSTGRES_IMAGE:-automation-postgres:18.4-v14}" 0.14.0
verify_running_image postgres_system postgres-standby \
  "${POSTGRES_IMAGE:-automation-postgres:18.4-v14}" 0.14.0
verify_running_image master_node master-node \
  "${MASTER_IMAGE:-automation-master-node:0.26.0}" 0.26.0 "$expected_control_plane_revision"
verify_running_image proxy_gateway proxy-gateway \
  "${PROXY_IMAGE:-automation-proxy-gateway:0.26.0}" 0.26.0 "$expected_control_plane_revision"
verify_running_image mcp_controller_server mcp-controller-server \
  "${MCP_CONTROLLER_IMAGE:-automation-mcp-controller-server:0.1.0}" \
  0.1.0 "${MCP_CONTROLLER_EXPECTED_REVISION:-v0001-r1}"

wait_for_health mqtt_broker mqtt-broker
wait_for_health postgres_system postgres-primary
wait_for_health postgres_system postgres-standby
wait_for_health master_node master-node
wait_for_health proxy_gateway proxy-gateway
wait_for_health mcp_controller_server mcp-controller-server
"$root_dir/master_node/test_runtime_state.sh"
"$root_dir/mcp_controller_server/ready.sh"

"$root_dir/postgres_system/scripts/verify_replication.sh"
(cd "$root_dir/master_node" && docker compose run --rm master-migrate)
(cd "$root_dir/proxy_gateway" && docker compose run --rm proxy-migrate)
"$root_dir/master_node/test_database.sh"
"$root_dir/proxy_gateway/test_database.sh"

if [[ "${CONTROL_PLANE_TEST_MODE:-0}" == "1" \
    && "${MQTT_SECURITY_TESTED:-0}" != "1" ]]; then
  [[ -e "$root_dir/mqtt_broker/test-pki/.test_mqtt_pki" ]] || {
    echo "CONTROL_PLANE_TEST_MODE requires the generated MQTT lab PKI." >&2
    exit 66
  }
  "$root_dir/mqtt_broker/test.sh"
fi

wait_for_health mqtt_broker mqtt-broker
wait_for_health master_node master-node
wait_for_health proxy_gateway proxy-gateway
wait_for_health mcp_controller_server mcp-controller-server
"$root_dir/master_node/test_runtime_state.sh"
if [[ "${RUN_E2E_TEST:-0}" == "1" ]]; then
  "$root_dir/test_e2e.sh"
fi
echo "All running-service health, MCP readiness, replication, migration and transaction probes passed."
