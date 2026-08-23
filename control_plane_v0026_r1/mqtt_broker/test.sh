#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pki_dir="${MQTT_PKI_WORK_DIR:-$root_dir/test-pki}"
host="${MQTT_TEST_HOST:-${MQTT_BIND_ADDRESS:-127.0.0.1}}"
port="${MQTT_PORT:-8883}"
cd "$root_dir"

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
[[ -e "$pki_dir/.test_mqtt_pki" ]] || {
  echo "Live MQTT tests require the generated lab PKI." >&2
  exit 66
}

client_directory() {
  case "$1" in
    master) echo "$pki_dir/clients/master" ;;
    proxy) echo "$pki_dir/clients/proxy" ;;
    mcp) echo "$pki_dir/clients/mcp-controller" ;;
    worker) echo "$pki_dir/clients/worker-fn-01" ;;
    worker-fn-02) echo "$pki_dir/clients/worker-fn-02" ;;
    health) echo "$pki_dir/clients/health" ;;
    *) echo "Unknown MQTT test identity: $1" >&2; return 64 ;;
  esac
}

run_client() {
  local identity="$1"
  local program="$2"
  shift 2
  local directory
  directory="$(client_directory "$identity")"
  docker compose --profile tools run --rm --no-deps \
    --user "$(id -u):$(id -g)" \
    --volume "$pki_dir:/test-pki:ro" \
    --entrypoint "$program" mqtt-tools \
    -h mqtt-broker -p 8883 -V 5 \
    -i "cp-${identity}-$$" \
    --cafile /test-pki/ca/ca.crt \
    --cert "/test-pki/${directory#"$pki_dir/"}/mqtt.crt" \
    --key "/test-pki/${directory#"$pki_dir/"}/mqtt.key" "$@"
}

source "$root_dir/scripts/mqtt_acl_probe.sh"
source "$root_dir/scripts/mqtt_delivery_probe.sh"

expect_denied() {
  if "$@" >/dev/null 2>&1; then
    echo "Expected MQTT authorization failure did not occur." >&2
    exit 1
  fi
}

broker_id="$(docker compose ps -q mqtt-broker)"
[[ -n "$broker_id" ]] || { echo "MQTT Broker is not running." >&2; exit 69; }
[[ "$(docker inspect --format '{{.State.Health.Status}}' "$broker_id")" == "healthy" ]]

openssl s_client -connect "$host:$port" -servername mqtt-broker \
  -verify_hostname mqtt-broker -verify_return_error \
  -CAfile "$pki_dir/ca/ca.crt" \
  -cert "$pki_dir/clients/health/mqtt.crt" \
  -key "$pki_dir/clients/health/mqtt.key" </dev/null 2>&1 \
  | grep -F 'Verify return code: 0 (ok)' >/dev/null

run_client health mosquitto_sub -q 0 -t '$SYS/broker/uptime' -C 1 -W 5 >/dev/null
expect_denied docker compose --profile tools run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$pki_dir:/test-pki:ro" --entrypoint mosquitto_sub mqtt-tools \
  -h mqtt-broker -p 8883 -V 5 -i cp-unauthenticated \
  --cafile /test-pki/ca/ca.crt \
  -t '$SYS/broker/uptime' -C 1 -W 3

run_client proxy mosquitto_pub -q 1 \
  -t automation/v1/site-main/master/master-01/commands/test/ping \
  -m '{"test":true}' >/dev/null
run_client mcp mosquitto_pub -q 1 \
  -t automation/v1/site-main/master/master-01/commands/test/mcp-ping \
  -m '{"test":true,"source":"mcp"}' >/dev/null
mcp_ack_payload="mcp-ack-acl-$(date +%s)-$$"
verify_exact_mqtt_delivery master mcp \
  automation/v1/site-main/proxy/mcp-controller-01/acks/master-01/test \
  "$mcp_ack_payload"
verify_publish_denied worker mcp master \
  "automation/v1/site-main/nodes/fn-01/commands/mcp-deny-$$"
verify_publish_denied worker proxy master \
  "automation/v1/site-main/nodes/fn-01/commands/test/deny-$$"
run_client worker mosquitto_pub -q 1 -r \
  -t automation/v1/site-main/nodes/fn-01/status -m 'online-test' >/dev/null
heartbeat_probe="worker-heartbeat-acl-$(date +%s)-$$"
run_client worker mosquitto_pub -q 1 -r \
  -t automation/v1/site-main/nodes/fn-01/heartbeat \
  -m "$heartbeat_probe" >/dev/null
received="$(run_client master mosquitto_sub -q 1 \
  -t automation/v1/site-main/nodes/fn-01/heartbeat -C 1 -W 5 | tail -n 1)"
[[ "$received" == "$heartbeat_probe" ]]
run_client worker mosquitto_pub -q 1 -r -n \
  -t automation/v1/site-main/nodes/fn-01/heartbeat >/dev/null
verify_publish_denied proxy worker master \
  "automation/v1/site-main/events/security-$$/master-01"
verify_subscription_denied proxy worker \
  "automation/v1/site-main/events/sensor-value/fn-01"
cross_fact_payload="cross-fact-acl-$(date +%s)-$$"
verify_exact_mqtt_delivery worker master \
  automation/v1/site-main/events/sensor_value_update/master-01 \
  "$cross_fact_payload"
verify_publish_denied worker-fn-02 worker master \
  "automation/v1/site-main/nodes/fn-02/commands/security-$$"

persistence_topic="automation/v1/site-main/events/mqtt_persistence_probe_$(date +%s)-$$"
payload="mqtt-persistence-$(date +%s)-$$"
run_client master mosquitto_pub -q 1 -r \
  -t "$persistence_topic" -m "$payload" >/dev/null
received="$(run_client master mosquitto_sub -q 1 \
  -t "$persistence_topic" -C 1 -W 5 | tail -n 1)"
[[ "$received" == "$payload" ]]

docker compose restart mqtt-broker >/dev/null
health=""
for _attempt in $(seq 1 30); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "$broker_id" 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  sleep 2
done
[[ "$health" == "healthy" ]]
received="$(run_client master mosquitto_sub -q 1 \
  -t "$persistence_topic" -C 1 -W 5 | tail -n 1)"
[[ "$received" == "$payload" ]]
run_client master mosquitto_pub -q 1 -r -n \
  -t "$persistence_topic" >/dev/null
run_client worker mosquitto_pub -q 1 -r -n \
  -t automation/v1/site-main/nodes/fn-01/status >/dev/null

if timeout 2 bash -c "</dev/tcp/$host/1883" 2>/dev/null; then
  echo "Unexpected plaintext MQTT listener detected on port 1883." >&2
  exit 1
fi
echo "MQTTS, isolated MCP identity, worker-proof ACL, allow/deny and persistence tests passed."
