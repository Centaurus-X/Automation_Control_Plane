#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${MQTT_PKI_WORK_DIR:-$root_dir/test-pki}"
secret_dir="${MQTT_SECRET_DIR:-$root_dir/secrets}"
ca_file="$work_dir/ca/ca.crt"

require_file() {
  [[ -s "$1" ]] || { echo "Required PKI file is missing: $1" >&2; exit 66; }
}

verify_pair() {
  local certificate="$1"
  local key="$2"
  local cert_digest
  local key_digest
  cert_digest="$(openssl x509 -in "$certificate" -pubkey -noout | openssl sha256)"
  key_digest="$(openssl pkey -in "$key" -pubout 2>/dev/null | openssl sha256)"
  [[ "$cert_digest" == "$key_digest" ]] || {
    echo "Certificate and key do not match: $certificate" >&2
    exit 65
  }
}

verify_client() {
  local directory="$1"
  local common_name="$2"
  require_file "$directory/mqtt.crt"
  require_file "$directory/mqtt.key"
  openssl verify -purpose sslclient -CAfile "$ca_file" "$directory/mqtt.crt" >/dev/null
  openssl x509 -checkend 604800 -noout -in "$directory/mqtt.crt" >/dev/null
  openssl x509 -noout -subject -nameopt RFC2253 -in "$directory/mqtt.crt" \
    | grep -F "CN=$common_name" >/dev/null
  verify_pair "$directory/mqtt.crt" "$directory/mqtt.key"
}

require_file "$ca_file"
require_file "$work_dir/server/mqtt_broker.crt"
require_file "$work_dir/server/mqtt_broker.key"
openssl verify -purpose sslserver -CAfile "$ca_file" \
  "$work_dir/server/mqtt_broker.crt" >/dev/null
openssl x509 -checkhost mqtt-broker -noout \
  -in "$work_dir/server/mqtt_broker.crt" >/dev/null
openssl x509 -checkend 604800 -noout \
  -in "$work_dir/server/mqtt_broker.crt" >/dev/null
verify_pair "$work_dir/server/mqtt_broker.crt" "$work_dir/server/mqtt_broker.key"

verify_client "$work_dir/clients/master" \
  "urn:automation:site-main:master:master-01"
verify_client "$work_dir/clients/proxy" \
  "urn:automation:site-main:proxy:proxy-01"
verify_client "$work_dir/clients/mcp-controller" \
  "urn:automation:site-main:proxy:mcp-controller-01"
verify_client "$work_dir/clients/worker-fn-01" \
  "urn:automation:site-main:worker:fn-01"
verify_client "$work_dir/clients/worker-fn-02" \
  "urn:automation:site-main:worker:fn-02"
verify_client "$work_dir/clients/health" \
  "urn:automation:site-main:monitor:mqtt-health"

for name in mqtt_ca.crt mqtt_broker.crt mqtt_broker.key mqtt_health.crt mqtt_health.key; do
  require_file "$secret_dir/$name"
done
cmp -s "$ca_file" "$secret_dir/mqtt_ca.crt"
cmp -s "$work_dir/server/mqtt_broker.crt" "$secret_dir/mqtt_broker.crt"
cmp -s "$work_dir/server/mqtt_broker.key" "$secret_dir/mqtt_broker.key"
cmp -s "$work_dir/clients/health/mqtt.crt" "$secret_dir/mqtt_health.crt"
cmp -s "$work_dir/clients/health/mqtt.key" "$secret_dir/mqtt_health.key"

[[ "$(stat -c '%a' "$secret_dir/mqtt_broker.key")" == "600" ]]
[[ "$(stat -c '%a' "$secret_dir/mqtt_health.key")" == "600" ]]
echo "MQTT certificate chain, identities, hostnames, key pairs and permissions are valid."
