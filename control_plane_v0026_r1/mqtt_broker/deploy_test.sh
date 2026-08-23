#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export ALLOW_TEST_MQTT_PKI=1
export MQTT_BIND_ADDRESS="${MQTT_BIND_ADDRESS:-127.0.0.1}"
export MQTT_PORT="${MQTT_PORT:-8883}"
"$root_dir/scripts/generate_test_pki.sh"
exec "$root_dir/deploy.sh" "$@"
