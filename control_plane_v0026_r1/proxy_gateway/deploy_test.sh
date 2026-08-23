#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset ALLOW_INSECURE_MQTT_TEST
export ALLOW_TEST_HTTPS_CERT=1
export MQTT_TLS_ENABLED=true
export MQTT_ALLOW_INSECURE_TRANSPORT=false
export MQTT_REQUIRED=true
export MQTT_HOST="${MQTT_HOST:-mqtt-broker}"
export MQTT_PORT="${MQTT_PORT:-8883}"
export MQTT_CA_FILE="${MQTT_CA_FILE:-$root_dir/../mqtt_broker/test-pki/ca/ca.crt}"
export PROXY_MQTT_CERT_FILE="${PROXY_MQTT_CERT_FILE:-$root_dir/../mqtt_broker/test-pki/clients/proxy/mqtt.crt}"
export PROXY_MQTT_KEY_FILE="${PROXY_MQTT_KEY_FILE:-$root_dir/../mqtt_broker/test-pki/clients/proxy/mqtt.key}"
exec "$root_dir/deploy.sh" "$@"
