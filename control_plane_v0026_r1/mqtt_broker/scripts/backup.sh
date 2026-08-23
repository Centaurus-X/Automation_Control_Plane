#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
backup_dir="${MQTT_BACKUP_DIR:-$root_dir/backup}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/mosquitto-$timestamp.db"
cd "$root_dir"
install -d -m 0750 "$backup_dir"
docker compose exec -T mqtt-broker sh -c 'kill -USR1 1'
sleep 2
docker compose cp mqtt-broker:/mosquitto/data/mosquitto.db "$target"
[[ -s "$target" ]] || { echo "MQTT persistence backup is empty." >&2; exit 1; }
chmod 0640 "$target"
sha256sum "$target" > "$target.sha256"
echo "MQTT persistence backup created: $target"
