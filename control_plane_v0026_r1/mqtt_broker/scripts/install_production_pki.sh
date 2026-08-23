#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/secrets"
install -d -m 0700 "$secret_dir"

install_required() {
  local source="$1"
  local target="$2"
  local mode="$3"
  [[ -n "$source" && -s "$source" ]] || {
    echo "Required production PKI source is missing for $target" >&2
    exit 66
  }
  install -m "$mode" "$source" "$secret_dir/$target"
}

install_required "${MQTT_CA_SOURCE:-}" mqtt_ca.crt 0644
install_required "${MQTT_BROKER_CERT_SOURCE:-}" mqtt_broker.crt 0644
install_required "${MQTT_BROKER_KEY_SOURCE:-}" mqtt_broker.key 0600
install_required "${MQTT_HEALTH_CERT_SOURCE:-}" mqtt_health.crt 0644
install_required "${MQTT_HEALTH_KEY_SOURCE:-}" mqtt_health.key 0600
rm -f "$secret_dir/.test_mqtt_pki"

openssl verify -purpose sslserver -CAfile "$secret_dir/mqtt_ca.crt" \
  "$secret_dir/mqtt_broker.crt" >/dev/null
openssl x509 -checkhost mqtt-broker -noout \
  -in "$secret_dir/mqtt_broker.crt" >/dev/null
openssl verify -purpose sslclient -CAfile "$secret_dir/mqtt_ca.crt" \
  "$secret_dir/mqtt_health.crt" >/dev/null
openssl x509 -noout -subject -nameopt RFC2253 -in "$secret_dir/mqtt_health.crt" \
  | grep -F 'CN=urn:automation:site-main:monitor:mqtt-health' >/dev/null
echo "Production MQTT PKI was installed and verified."
