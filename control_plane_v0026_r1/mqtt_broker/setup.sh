#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
secret_dir="$root_dir/secrets"
image="${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
cd "$root_dir"

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 69; }
docker compose version >/dev/null
[[ "${MQTT_PORT:-8883}" == "8883" ]] || {
  echo "This release exposes the secure MQTT listener only on port 8883." >&2
  exit 64
}

for name in mqtt_ca.crt mqtt_broker.crt mqtt_broker.key mqtt_health.crt mqtt_health.key; do
  [[ -s "$secret_dir/$name" ]] || {
    echo "Required MQTT runtime secret is missing: $secret_dir/$name" >&2
    exit 66
  }
done
if [[ -e "$secret_dir/.test_mqtt_pki" && "${ALLOW_TEST_MQTT_PKI:-0}" != "1" ]]; then
  echo "Refusing test MQTT PKI in production mode." >&2
  exit 78
fi

openssl verify -purpose sslserver -CAfile "$secret_dir/mqtt_ca.crt" \
  "$secret_dir/mqtt_broker.crt" >/dev/null
openssl x509 -checkhost mqtt-broker -noout -in "$secret_dir/mqtt_broker.crt" >/dev/null
openssl verify -purpose sslclient -CAfile "$secret_dir/mqtt_ca.crt" \
  "$secret_dir/mqtt_health.crt" >/dev/null

if ! docker network inspect automation_control >/dev/null 2>&1; then
  docker network create --driver bridge automation_control >/dev/null
fi
network_driver="$(docker network inspect --format '{{.Driver}}' automation_control)"
[[ "$network_driver" == "bridge" ]] || {
  echo "automation_control must use the bridge driver on this single-host release." >&2
  exit 78
}
if [[ "${CONTROL_PLANE_IMAGES_PREPARED:-0}" != "1" ]]; then
  docker pull "$image"
fi
"$root_dir/scripts/prepare_runtime_config.sh"
docker compose config --quiet
docker run --rm \
  --volume "$root_dir/runtime-config:/mosquitto/config:ro" \
  --volume "$secret_dir:/run/secrets:ro" \
  "$image" mosquitto --test-config -c /mosquitto/config/mosquitto.conf
echo "MQTT Broker prerequisites and configuration are valid."
