#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
"$root_dir/setup.sh"
"$root_dir/scripts/prepare_compose_secrets.sh" \
  "$root_dir/secrets" 1883 "${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
docker compose up -d --force-recreate --remove-orphans mqtt-broker

health=""
for _attempt in $(seq 1 30); do
  broker_id="$(docker compose ps -q mqtt-broker)"
  health="$(docker inspect --format '{{.State.Health.Status}}' "$broker_id" 2>/dev/null || true)"
  [[ "$health" == "healthy" ]] && break
  sleep 2
done
if [[ "$health" != "healthy" ]]; then
  echo "MQTT Broker did not become healthy." >&2
  docker compose logs --tail=100 mqtt-broker >&2 || true
  exit 1
fi
echo "MQTT Broker is healthy on MQTTS port ${MQTT_PORT:-8883}."
