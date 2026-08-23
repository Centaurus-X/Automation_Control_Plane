#!/usr/bin/env bash

set -Eeuo pipefail
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$root_dir/scripts/control_plane_lifecycle.sh"

case "${1:-}" in
  --fresh)
    deployment_mode=fresh
    shift
    [[ "${CONTROL_PLANE_CONFIRM_PURGE:-}" == "YES" ]] || {
      echo "Production --fresh permanently removes Control Plane volumes." >&2
      echo "Set CONTROL_PLANE_CONFIRM_PURGE=YES to confirm." >&2
      exit 64
    }
    ;;
  --upgrade)
    deployment_mode=upgrade
    shift
    [[ -n "${CONTROL_PLANE_STATE_SOURCE:-}" ]] || {
      echo "Production --upgrade requires CONTROL_PLANE_STATE_SOURCE." >&2
      exit 64
    }
    ;;
  -h|--help)
    echo "Usage: $0 {--fresh|--upgrade}"
    echo "  --fresh    purge volumes; requires CONTROL_PLANE_CONFIRM_PURGE=YES"
    echo "  --upgrade  preserve volumes; requires CONTROL_PLANE_STATE_SOURCE"
    exit 0
    ;;
  *)
    echo "Production rollout requires an explicit --fresh or --upgrade mode." >&2
    exit 64
    ;;
esac
(( $# == 0 )) || { echo "Unexpected production arguments: $*" >&2; exit 64; }

required_variables=(
  MQTT_BIND_ADDRESS PROXY_BIND_ADDRESS PROXY_TLS_SERVER_NAME MOSQUITTO_IMAGE
  PROXY_ORIGIN_ALLOWLIST MCP_BIND_ADDRESS MCP_AUTH_SOURCE
  MQTT_CA_SOURCE MQTT_BROKER_CERT_SOURCE MQTT_BROKER_KEY_SOURCE
  MQTT_HEALTH_CERT_SOURCE MQTT_HEALTH_KEY_SOURCE
  MASTER_MQTT_CERT_FILE MASTER_MQTT_KEY_FILE
  PROXY_MQTT_CERT_FILE PROXY_MQTT_KEY_FILE
  MCP_MQTT_CERT_SOURCE MCP_MQTT_KEY_SOURCE
  POSTGRES_CA_SOURCE POSTGRES_PRIMARY_CERT_SOURCE POSTGRES_PRIMARY_KEY_SOURCE
  POSTGRES_STANDBY_CERT_SOURCE POSTGRES_STANDBY_KEY_SOURCE
  PROXY_HTTPS_CERT_SOURCE PROXY_HTTPS_KEY_SOURCE
  MASTER_INITIAL_CONFIG_SOURCE
)
for variable in "${required_variables[@]}"; do
  [[ -n "${!variable:-}" ]] || {
    echo "Required production variable is unset: $variable" >&2
    exit 64
  }
done
[[ "$MOSQUITTO_IMAGE" == *@sha256:* ]] || {
  echo "Production MOSQUITTO_IMAGE must be pinned by sha256 digest." >&2
  exit 64
}
unset CONTROL_PLANE_TEST_MODE
export AUTOMATION_SITE_ID="${AUTOMATION_SITE_ID:-site-main}"
export MCP_HTTP_PORT="${MCP_HTTP_PORT:-8080}"
export MCP_MQTT_SOURCE_INSTANCE_ID="${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}"
export MCP_MQTT_CA_SOURCE="$MQTT_CA_SOURCE"
export PROXY_ORIGIN_ALLOWLIST MCP_BIND_ADDRESS MCP_AUTH_SOURCE
validate_proxy_deployment_config "$root_dir"

require_control_plane_tools
"$root_dir/tools/verify_checksums.sh"
"$root_dir/test_static_all.sh"

verify_key_pair() {
  local certificate="$1"
  local key="$2"
  local certificate_digest
  local key_digest
  certificate_digest="$(openssl x509 -in "$certificate" -pubkey -noout | openssl sha256)"
  key_digest="$(openssl pkey -in "$key" -pubout 2>/dev/null | openssl sha256)"
  [[ "$certificate_digest" == "$key_digest" ]] || {
    echo "Certificate and private key do not match: $certificate" >&2
    exit 65
  }
}

openssl verify -purpose sslserver -CAfile "$MQTT_CA_SOURCE" \
  "$MQTT_BROKER_CERT_SOURCE" >/dev/null
openssl x509 -checkhost mqtt-broker -noout -in "$MQTT_BROKER_CERT_SOURCE" >/dev/null
for client_file in "$MASTER_MQTT_CERT_FILE" "$PROXY_MQTT_CERT_FILE" \
    "$MCP_MQTT_CERT_SOURCE" "$MQTT_HEALTH_CERT_SOURCE"; do
  openssl verify -purpose sslclient -CAfile "$MQTT_CA_SOURCE" "$client_file" >/dev/null
done
openssl x509 -noout -subject -nameopt RFC2253 -in "$MASTER_MQTT_CERT_FILE" \
  | grep -F 'CN=urn:automation:site-main:master:master-01' >/dev/null
openssl x509 -noout -subject -nameopt RFC2253 -in "$PROXY_MQTT_CERT_FILE" \
  | grep -F 'CN=urn:automation:site-main:proxy:proxy-01' >/dev/null
openssl x509 -noout -subject -nameopt RFC2253 -in "$MCP_MQTT_CERT_SOURCE" \
  | grep -F "CN=urn:automation:${AUTOMATION_SITE_ID}:proxy:${MCP_MQTT_SOURCE_INSTANCE_ID}" >/dev/null
openssl x509 -noout -subject -nameopt RFC2253 -in "$MQTT_HEALTH_CERT_SOURCE" \
  | grep -F 'CN=urn:automation:site-main:monitor:mqtt-health' >/dev/null
openssl verify -CAfile "$POSTGRES_CA_SOURCE" \
  "$POSTGRES_PRIMARY_CERT_SOURCE" "$POSTGRES_STANDBY_CERT_SOURCE" >/dev/null
openssl x509 -checkhost automation-postgres -noout \
  -in "$POSTGRES_PRIMARY_CERT_SOURCE" >/dev/null
openssl x509 -checkhost postgres-standby -noout \
  -in "$POSTGRES_STANDBY_CERT_SOURCE" >/dev/null
if [[ "$PROXY_TLS_SERVER_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  openssl x509 -checkip "$PROXY_TLS_SERVER_NAME" -noout \
    -in "$PROXY_HTTPS_CERT_SOURCE" >/dev/null
else
  openssl x509 -checkhost "$PROXY_TLS_SERVER_NAME" -noout \
    -in "$PROXY_HTTPS_CERT_SOURCE" >/dev/null
fi
for certificate in "$MQTT_BROKER_CERT_SOURCE" "$MQTT_HEALTH_CERT_SOURCE" \
    "$MASTER_MQTT_CERT_FILE" "$PROXY_MQTT_CERT_FILE" "$MCP_MQTT_CERT_SOURCE" \
    "$POSTGRES_PRIMARY_CERT_SOURCE" "$POSTGRES_STANDBY_CERT_SOURCE" \
    "$PROXY_HTTPS_CERT_SOURCE"; do
  openssl x509 -checkend 604800 -noout -in "$certificate" >/dev/null
done
verify_key_pair "$MQTT_BROKER_CERT_SOURCE" "$MQTT_BROKER_KEY_SOURCE"
verify_key_pair "$MQTT_HEALTH_CERT_SOURCE" "$MQTT_HEALTH_KEY_SOURCE"
verify_key_pair "$MASTER_MQTT_CERT_FILE" "$MASTER_MQTT_KEY_FILE"
verify_key_pair "$PROXY_MQTT_CERT_FILE" "$PROXY_MQTT_KEY_FILE"
verify_key_pair "$MCP_MQTT_CERT_SOURCE" "$MCP_MQTT_KEY_SOURCE"
verify_key_pair "$POSTGRES_PRIMARY_CERT_SOURCE" "$POSTGRES_PRIMARY_KEY_SOURCE"
verify_key_pair "$POSTGRES_STANDBY_CERT_SOURCE" "$POSTGRES_STANDBY_KEY_SOURCE"
verify_key_pair "$PROXY_HTTPS_CERT_SOURCE" "$PROXY_HTTPS_KEY_SOURCE"
[[ -f "$MCP_AUTH_SOURCE" && ! -L "$MCP_AUTH_SOURCE" && -s "$MCP_AUTH_SOURCE" ]] || {
  echo "MCP_AUTH_SOURCE must be a non-empty regular file." >&2
  exit 65
}
auth_mode="$(stat -c %a "$MCP_AUTH_SOURCE")"
(( (8#$auth_mode & 077) == 0 )) || {
  echo "MCP_AUTH_SOURCE permissions must be 0600 or stricter." >&2
  exit 65
}

acquire_control_plane_lock
cleanup() {
  result="$?"
  if (( result != 0 )); then
    echo "Production rollout failed in stage: ${CONTROL_PLANE_CURRENT_STAGE:-unknown}." >&2
    echo "Current container state follows." >&2
    print_control_plane_container_status >&2 || true
  fi
  release_control_plane_lock
  exit "$result"
}
trap cleanup EXIT

export MQTT_CA_FILE="$MQTT_CA_SOURCE"
export MQTT_HOST=mqtt-broker
export MQTT_PORT=8883
export MQTT_TLS_ENABLED=true
export MQTT_ALLOW_INSECURE_TRANSPORT=false
export MQTT_REQUIRED=true
export MASTER_RELEASE_PROFILE=production_v2
export MASTER_AUTO_ADMIT_BROKER_BOUND_WORKERS=false
export MASTER_INITIAL_CONFIG_FILE="$MASTER_INITIAL_CONFIG_SOURCE"
validate_control_plane_compose_files "$root_dir"

PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$root_dir/master_node/master_node:$root_dir/master_node/master_node/src" \
python3 "$root_dir/tools/validate_initial_config.py" \
  --profile production_v2 "$MASTER_INITIAL_CONFIG_SOURCE"

echo "Selected production deployment mode: $deployment_mode"
if [[ "$deployment_mode" == "fresh" ]]; then
  run_rollout_stage "1/9 fresh purge" \
    prepare_control_plane_deployment "$deployment_mode" "$root_dir" ""
  run_rollout_stage "2/9 image staging" prepare_control_plane_images "$root_dir"
else
  run_rollout_stage "1/9 image staging" prepare_control_plane_images "$root_dir"
  run_rollout_stage "2/9 upgrade deployment preparation" \
    prepare_control_plane_deployment "$deployment_mode" "$root_dir" \
    "${CONTROL_PLANE_STATE_SOURCE:-}"
fi
run_rollout_stage "3/9 MQTT PKI installation" \
  "$root_dir/mqtt_broker/scripts/install_production_pki.sh"
run_rollout_stage "4/9 MQTT deployment" "$root_dir/mqtt_broker/deploy.sh"
run_rollout_stage "5/9 PostgreSQL deployment" "$root_dir/postgres_system/deploy.sh"
run_rollout_stage "6/9 Master deployment" "$root_dir/master_node/deploy.sh"
run_rollout_stage "7/9 Proxy deployment" "$root_dir/proxy_gateway/deploy.sh"
export MCP_CONTROLLER_SKIP_BUILD=1
run_rollout_stage "8/9 MCP Controller Server deployment" "$root_dir/mcp_controller_server/deploy.sh"
run_rollout_stage "9/9 running-system acceptance tests" "$root_dir/test_running_all.sh"
echo "Control Plane 0.26.0/v0026-r1 with MCP Controller Server 0.1.0/v0001-r1 production rollout completed successfully ($deployment_mode mode)."
