#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/secrets"
postgres_dir="${POSTGRES_SYSTEM_DIR:-$root_dir/../postgres_system}"
install -d -m 0700 "$secret_dir"

install_required() {
  local source="$1"
  local target="$2"
  [[ -s "$source" ]] || { echo "Required secret source is missing: $source" >&2; exit 66; }
  install -m 0600 "$source" "$secret_dir/$target"
}

install_required "$postgres_dir/secrets/master_app_password" master_db_password
install_required "$postgres_dir/secrets/master_migrator_password" master_migrator_password
install_required "$postgres_dir/tls/ca.crt" postgres_ca.crt

if [[ "${ALLOW_INSECURE_MQTT_TEST:-0}" == "1" ]]; then
  for name in mqtt_ca.crt master_mqtt.crt master_mqtt.key; do
    if [[ ! -e "$secret_dir/$name" ]]; then
      umask 077
      printf 'test-only-placeholder\n' > "$secret_dir/$name"
    fi
  done
else
  install_required "${MQTT_CA_FILE:-}" mqtt_ca.crt
  install_required "${MASTER_MQTT_CERT_FILE:-}" master_mqtt.crt
  install_required "${MASTER_MQTT_KEY_FILE:-}" master_mqtt.key
fi

echo "Master secret files are ready."
