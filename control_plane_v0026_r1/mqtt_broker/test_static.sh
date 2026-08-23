#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
temporary_dir="$(mktemp -d)"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

bash -n "$root_dir"/*.sh "$root_dir"/scripts/*.sh
grep -F 'eclipse-mosquitto:2.1.2-alpine' "$root_dir/compose.yaml" >/dev/null
grep -F 'listener 8883 0.0.0.0' "$root_dir/config/mosquitto.conf" >/dev/null
grep -F 'listener_allow_anonymous false' "$root_dir/config/mosquitto.conf" >/dev/null
grep -F 'require_certificate true' "$root_dir/config/mosquitto.conf" >/dev/null
grep -F 'use_identity_as_username true' "$root_dir/config/mosquitto.conf" >/dev/null
grep -F 'max_qos 1' "$root_dir/config/mosquitto.conf" >/dev/null
grep -F -- '-i automation-mqtt-health' "$root_dir/compose.yaml" >/dev/null
grep -F -- '--user "$(id -u):$(id -g)"' "$root_dir/test.sh" >/dev/null
grep -F 'source "$root_dir/scripts/mqtt_acl_probe.sh"' "$root_dir/test.sh" >/dev/null
grep -F 'nodes/fn-01/heartbeat -C 1' "$root_dir/test.sh" >/dev/null
grep -F 'MQTT ACL violation' "$root_dir/scripts/mqtt_acl_probe.sh" >/dev/null
grep -F 'verify_subscription_denied proxy worker' "$root_dir/test.sh" >/dev/null
grep -F 'verify_exact_mqtt_delivery master mcp' "$root_dir/test.sh" >/dev/null
grep -F 'verify_publish_denied worker mcp master' "$root_dir/test.sh" >/dev/null
grep -F 'events/security-$$/master-01' "$root_dir/test.sh" >/dev/null
if grep -F 'events/security-$$/fn-02' "$root_dir/test.sh" >/dev/null; then
  echo "Proxy ACL probe still uses a Worker event topic as its allowed read path." >&2
  exit 1
fi
grep -F 'prepare_compose_secrets.sh' "$root_dir/deploy.sh" >/dev/null
grep -F 'prepare_runtime_config.sh' "$root_dir/setup.sh" >/dev/null
grep -F './runtime-config:/mosquitto/config:ro' "$root_dir/compose.yaml" >/dev/null
grep -F 'topic read automation/v1/site-main/broadcast/commands/#' \
  "$root_dir/config/acl.conf" >/dev/null
grep -F 'topic read automation/v1/site-main/nodes/+/heartbeat' \
  "$root_dir/config/acl.conf" >/dev/null
grep -F 'topic read automation/v1/site-main/events/+/master-01' \
  "$root_dir/config/acl.conf" >/dev/null
if awk '/user urn:automation:site-main:proxy:proxy-01/{proxy=1;next} /^user /{proxy=0} proxy' \
    "$root_dir/config/acl.conf" | grep -F 'events/#' >/dev/null; then
  echo "Proxy ACL still grants the legacy Worker event namespace." >&2
  exit 1
fi
if grep -R -E 'listener[[:space:]]+1883|listener_allow_anonymous[[:space:]]+true' \
    "$root_dir/config" >/dev/null; then
  echo "An insecure MQTT listener or anonymous access was found." >&2
  exit 1
fi
if grep -E '^(per_listener_settings|message_size_limit)[[:space:]]' \
    "$root_dir/config/mosquitto.conf" >/dev/null; then
  echo "Deprecated Mosquitto 2.1 configuration remains active." >&2
  exit 1
fi
for identity in \
  'urn:automation:site-main:master:master-01' \
  'urn:automation:site-main:proxy:proxy-01' \
  'urn:automation:site-main:proxy:mcp-controller-01' \
  'urn:automation:site-main:worker:fn-01' \
  'urn:automation:site-main:worker:fn-02' \
  'urn:automation:site-main:monitor:mqtt-health'; do
  grep -F "user $identity" "$root_dir/config/acl.conf" >/dev/null
done
grep -F 'topic read automation/v1/site-main/proxy/mcp-controller-01/acks/#' \
  "$root_dir/config/acl.conf" >/dev/null
grep -F 'topic write automation/v1/site-main/proxy/mcp-controller-01/acks/#' \
  "$root_dir/config/acl.conf" >/dev/null

MQTT_PKI_WORK_DIR="$temporary_dir/test-pki" \
MQTT_SECRET_DIR="$temporary_dir/secrets" \
MQTT_TEST_BROKER_IP=192.168.0.82 \
  "$root_dir/scripts/generate_test_pki.sh"
MQTT_PKI_WORK_DIR="$temporary_dir/test-pki" \
MQTT_SECRET_DIR="$temporary_dir/secrets" \
  "$root_dir/scripts/generate_test_pki.sh"

if command -v docker >/dev/null 2>&1; then
  docker compose -f "$root_dir/compose.yaml" config --quiet
fi
echo "MQTT configuration, ACL, scripts and idempotent test PKI checks passed."
