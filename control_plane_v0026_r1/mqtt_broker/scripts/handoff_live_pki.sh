#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
profile="${1:-}"
broker_ip="${MQTT_TEST_BROKER_IP:-${MQTT_BIND_ADDRESS:-}}"

fail() {
  echo "MQTT live-PKI handoff failed: $1" >&2
  exit 78
}

directory_guard="$root_dir/scripts/runtime_directory_guard.sh"
[[ -f "$directory_guard" && ! -L "$directory_guard" ]] || \
  fail "safe runtime-directory guard is missing"
source "$directory_guard"

[[ "$profile" == "test" || "$profile" == "production" ]] || \
  fail "expected deployment profile test or production"
command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"

single_container() {
  local project="$1"
  local service="$2"
  local values
  values="$(docker ps -q \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$service")"
  [[ "$(printf '%s\n' "$values" | sed '/^$/d' | wc -l)" -le 1 ]] || \
    fail "multiple active containers for $project/$service"
  printf '%s' "$values"
}

assert_healthy() {
  local container_id="$1"
  local label="$2"
  local observed
  observed="$(docker inspect --format \
    '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$container_id")"
  [[ "$observed" == "running|healthy" || "$observed" == "running|" ]] || \
    fail "$label is not healthy: $observed"
}

mount_source() {
  local container_id="$1"
  local destination="$2"
  local source
  source="$(docker inspect --format \
    "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{println .Source}}{{end}}{{end}}" \
    "$container_id")"
  [[ "$source" = /* && -f "$source" && ! -L "$source" && -s "$source" ]] || \
    fail "unsafe or missing active mount source for $destination"
  printf '%s' "$source"
}

verify_pair() {
  local ca_file="$1"
  local cert_file="$2"
  local key_file="$3"
  local purpose="$4"
  local expected_identity="$5"
  local cert_public
  local key_public
  openssl verify -purpose "$purpose" -CAfile "$ca_file" \
    "$cert_file" >/dev/null || fail "certificate-chain verification failed"
  openssl x509 -checkend 86400 -noout -in "$cert_file" >/dev/null || \
    fail "certificate expires within 24 hours"
  openssl x509 -noout -ext subjectAltName -in "$cert_file" \
    | grep -F "$expected_identity" >/dev/null || \
    fail "certificate identity mismatch: $expected_identity"
  cert_public="$(openssl x509 -in "$cert_file" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  key_public="$(openssl pkey -in "$key_file" -pubout -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  [[ -n "$cert_public" && "$cert_public" == "$key_public" ]] || \
    fail "certificate/private-key mismatch: $expected_identity"
}

copy_public() {
  install -m 0644 "$1" "$2"
}

copy_private() {
  install -m 0600 "$1" "$2"
}

broker_id="$(single_container automation-mqtt mqtt-broker)"
master_id="$(single_container automation-master master-node)"
proxy_id="$(single_container automation-proxy proxy-gateway)"
mcp_id="$(single_container automation-mcp-controller mcp-controller-server)"

if [[ -z "$broker_id" && -z "$master_id" && -z "$proxy_id" ]]; then
  echo "No active MQTT trust domain was found; fresh provisioning will be used."
  exit 0
fi
[[ -n "$broker_id" && -n "$master_id" && -n "$proxy_id" ]] || \
  fail "active MQTT, Master and Proxy containers must form one complete chain"
assert_healthy "$broker_id" mqtt-broker
assert_healthy "$master_id" master-node
assert_healthy "$proxy_id" proxy-gateway
if [[ -n "$mcp_id" ]]; then
  assert_healthy "$mcp_id" mcp-controller-server
fi

broker_ca="$(mount_source "$broker_id" /run/secrets/mqtt_ca)"
broker_cert="$(mount_source "$broker_id" /run/secrets/mqtt_broker_cert)"
broker_key="$(mount_source "$broker_id" /run/secrets/mqtt_broker_key)"
health_cert="$(mount_source "$broker_id" /run/secrets/mqtt_health_cert)"
health_key="$(mount_source "$broker_id" /run/secrets/mqtt_health_key)"
master_ca="$(mount_source "$master_id" /run/secrets/mqtt_ca)"
master_cert="$(mount_source "$master_id" /run/secrets/master_mqtt_cert)"
master_key="$(mount_source "$master_id" /run/secrets/master_mqtt_key)"
proxy_ca="$(mount_source "$proxy_id" /run/secrets/mqtt_ca)"
proxy_cert="$(mount_source "$proxy_id" /run/secrets/proxy_mqtt_cert)"
proxy_key="$(mount_source "$proxy_id" /run/secrets/proxy_mqtt_key)"
mcp_ca=""
mcp_cert=""
mcp_key=""
if [[ -n "$mcp_id" ]]; then
  mcp_ca="$(mount_source "$mcp_id" /run/secrets/mqtt_ca)"
  mcp_cert="$(mount_source "$mcp_id" /run/secrets/mcp_mqtt_cert)"
  mcp_key="$(mount_source "$mcp_id" /run/secrets/mcp_mqtt_key)"
fi

cmp -s "$broker_ca" "$master_ca" || fail "Master MQTT CA differs from broker"
cmp -s "$broker_ca" "$proxy_ca" || fail "Proxy MQTT CA differs from broker"
if [[ -n "$mcp_ca" ]]; then
  cmp -s "$broker_ca" "$mcp_ca" || fail "MCP MQTT CA differs from broker"
fi
verify_pair "$broker_ca" "$broker_cert" "$broker_key" sslserver \
  "DNS:mqtt-broker"
verify_pair "$broker_ca" "$health_cert" "$health_key" sslclient \
  "URI:urn:automation:site-main:monitor:mqtt-health"
verify_pair "$broker_ca" "$master_cert" "$master_key" sslclient \
  "URI:urn:automation:site-main:master:master-01"
verify_pair "$broker_ca" "$proxy_cert" "$proxy_key" sslclient \
  "URI:urn:automation:site-main:proxy:proxy-01"
if [[ -n "$mcp_cert" ]]; then
  verify_pair "$broker_ca" "$mcp_cert" "$mcp_key" sslclient \
    "URI:urn:automation:site-main:proxy:mcp-controller-01"
fi
if [[ -n "$broker_ip" ]]; then
  openssl x509 -checkip "$broker_ip" -noout -in "$broker_cert" >/dev/null || \
    fail "active broker certificate does not cover $broker_ip"
fi

active_secret_dir="$(dirname "$broker_ca")"
active_test_pki="$(dirname "$active_secret_dir")/test-pki"
if [[ "$profile" == "production" && -e "$active_secret_dir/.test_mqtt_pki" ]]; then
  fail "test MQTT PKI cannot be promoted into a production deployment"
fi

stage="$(mktemp -d "$root_dir/.mqtt-pki-handoff.XXXXXX")"
cleanup() {
  if [[ -d "$stage" && "$stage" == "$root_dir"/.mqtt-pki-handoff.* ]]; then
    find "$stage" -depth -delete
  fi
}
trap cleanup EXIT
install -d -m 0700 "$stage/runtime-pki-handoff" "$stage/secrets"
copy_public "$broker_ca" "$stage/runtime-pki-handoff/mqtt_ca.crt"
copy_public "$broker_cert" "$stage/runtime-pki-handoff/mqtt_broker.crt"
copy_private "$broker_key" "$stage/runtime-pki-handoff/mqtt_broker.key"
copy_public "$health_cert" "$stage/runtime-pki-handoff/mqtt_health.crt"
copy_private "$health_key" "$stage/runtime-pki-handoff/mqtt_health.key"
copy_public "$master_cert" "$stage/runtime-pki-handoff/master_mqtt.crt"
copy_private "$master_key" "$stage/runtime-pki-handoff/master_mqtt.key"
copy_public "$proxy_cert" "$stage/runtime-pki-handoff/proxy_mqtt.crt"
copy_private "$proxy_key" "$stage/runtime-pki-handoff/proxy_mqtt.key"
if [[ -n "$mcp_cert" ]]; then
  copy_public "$mcp_cert" "$stage/runtime-pki-handoff/mcp_mqtt.crt"
  copy_private "$mcp_key" "$stage/runtime-pki-handoff/mcp_mqtt.key"
fi
touch "$stage/runtime-pki-handoff/.live_mqtt_pki_handoff"
chmod 0600 "$stage/runtime-pki-handoff/.live_mqtt_pki_handoff"

copy_public "$broker_ca" "$stage/secrets/mqtt_ca.crt"
copy_public "$broker_cert" "$stage/secrets/mqtt_broker.crt"
copy_private "$broker_key" "$stage/secrets/mqtt_broker.key"
copy_public "$health_cert" "$stage/secrets/mqtt_health.crt"
copy_private "$health_key" "$stage/secrets/mqtt_health.key"
touch "$stage/secrets/.live_mqtt_pki_handoff"
chmod 0600 "$stage/secrets/.live_mqtt_pki_handoff"

if [[ "$profile" == "test" ]]; then
  [[ -f "$active_secret_dir/.test_mqtt_pki" \
    && -f "$active_test_pki/.test_mqtt_pki" ]] || \
    fail "active test deployment has no complete marked test PKI"
  test_ca="$active_test_pki/ca/ca.crt"
  test_ca_key="$active_test_pki/ca/ca.key"
  test_server_cert="$active_test_pki/server/mqtt_broker.crt"
  test_server_key="$active_test_pki/server/mqtt_broker.key"
  [[ -s "$test_ca" && -s "$test_ca_key" ]] || fail "test CA is incomplete"
  cmp -s "$broker_ca" "$test_ca" || fail "active test PKI CA drifted"
  cmp -s "$broker_cert" "$test_server_cert" || fail "test broker cert drifted"
  cmp -s "$broker_key" "$test_server_key" || fail "test broker key drifted"
  install -d -m 0700 "$stage/test-pki/ca" "$stage/test-pki/server"
  copy_public "$test_ca" "$stage/test-pki/ca/ca.crt"
  copy_private "$test_ca_key" "$stage/test-pki/ca/ca.key"
  copy_public "$test_server_cert" "$stage/test-pki/server/mqtt_broker.crt"
  copy_private "$test_server_key" "$stage/test-pki/server/mqtt_broker.key"
  test_identities=(master proxy worker-fn-01 worker-fn-02 health)
  if [[ -s "$active_test_pki/clients/mcp-controller/mqtt.crt" \
      && -s "$active_test_pki/clients/mcp-controller/mqtt.key" ]]; then
    test_identities+=(mcp-controller)
  fi
  for identity in "${test_identities[@]}"; do
    source_dir="$active_test_pki/clients/$identity"
    target_dir="$stage/test-pki/clients/$identity"
    install -d -m 0700 "$target_dir"
    copy_public "$source_dir/mqtt.crt" "$target_dir/mqtt.crt"
    copy_private "$source_dir/mqtt.key" "$target_dir/mqtt.key"
  done
  cmp -s "$master_cert" "$active_test_pki/clients/master/mqtt.crt" || \
    fail "test Master identity differs from active identity"
  cmp -s "$proxy_cert" "$active_test_pki/clients/proxy/mqtt.crt" || \
    fail "test Proxy identity differs from active identity"
  if [[ -n "$mcp_cert" ]]; then
    cmp -s "$mcp_cert" "$active_test_pki/clients/mcp-controller/mqtt.crt" || \
      fail "test MCP identity differs from active identity"
  fi
  cmp -s "$health_cert" "$active_test_pki/clients/health/mqtt.crt" || \
    fail "test health identity differs from active identity"
  verify_pair "$test_ca" \
    "$active_test_pki/clients/worker-fn-01/mqtt.crt" \
    "$active_test_pki/clients/worker-fn-01/mqtt.key" sslclient \
    "URI:urn:automation:site-main:worker:fn-01"
  verify_pair "$test_ca" \
    "$active_test_pki/clients/worker-fn-02/mqtt.crt" \
    "$active_test_pki/clients/worker-fn-02/mqtt.key" sslclient \
    "URI:urn:automation:site-main:worker:fn-02"
  touch "$stage/test-pki/.test_mqtt_pki" "$stage/secrets/.test_mqtt_pki"
  chmod 0600 "$stage/test-pki/.test_mqtt_pki" \
    "$stage/secrets/.test_mqtt_pki"
fi

for destination in "$root_dir/runtime-pki-handoff" "$root_dir/secrets"; do
  [[ ! -L "$destination" ]] || \
    fail "runtime destination is a symlink: $destination"
  if [[ -d "$destination" \
    && ! -e "$destination/.live_mqtt_pki_handoff" \
    && -n "$(find "$destination" -mindepth 1 -print -quit)" ]]; then
    if mqtt_allow_verified_active_secret_destination \
        "$destination" "$active_secret_dir" "$root_dir/secrets" "$profile"; then
      echo "Verified active MQTT runtime secret directory is the handoff destination; idempotent convergence is allowed."
    else
      fail "refusing to overwrite unmarked runtime material: $destination"
    fi
  fi
done
install -d -m 0700 "$root_dir/runtime-pki-handoff" "$root_dir/secrets"
cp -a "$stage/runtime-pki-handoff/." "$root_dir/runtime-pki-handoff/"
cp -a "$stage/secrets/." "$root_dir/secrets/"
if [[ "$profile" == "test" ]]; then
  if [[ -d "$root_dir/test-pki" \
    && ! -e "$root_dir/test-pki/.test_mqtt_pki" \
    && -n "$(find "$root_dir/test-pki" -mindepth 1 -print -quit)" ]]; then
    fail "refusing to overwrite an unmarked test PKI directory"
  fi
  install -d -m 0700 "$root_dir/test-pki"
  cp -a "$stage/test-pki/." "$root_dir/test-pki/"
fi

echo "Active MQTT trust domain was verified and handed off for $profile rollout."
