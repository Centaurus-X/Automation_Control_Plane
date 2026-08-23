#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root_dir/mqtt_broker/scripts/runtime_directory_guard.sh"

fail() {
  echo "MQTT runtime-directory guard regression failed: $1" >&2
  exit 1
}

scratch="$(mktemp -d)"
cleanup() {
  if [[ -d "$scratch" ]]; then
    find "$scratch" -depth -delete
  fi
}
trap cleanup EXIT

active="$scratch/active"
different="$scratch/different"
install -d -m 0700 "$active" "$different"
for name in \
    mqtt_ca.crt \
    mqtt_broker.crt \
    mqtt_broker.key \
    mqtt_health.crt \
    mqtt_health.key; do
  printf 'regression-%s\n' "$name" >"$active/$name"
  printf 'regression-%s\n' "$name" >"$different/$name"
done
touch "$active/.test_mqtt_pki" "$different/.test_mqtt_pki"

mqtt_allow_verified_active_secret_destination \
  "$active" "$active" "$active" test || \
  fail "the exact verified active test directory was rejected"

if mqtt_allow_verified_active_secret_destination \
    "$different" "$active" "$different" test; then
  fail "a different physical directory was accepted"
fi

link="$scratch/active-link"
ln -s "$active" "$link"
if mqtt_allow_verified_active_secret_destination \
    "$link" "$active" "$link" test; then
  fail "a symlink destination was accepted"
fi

find "$active/mqtt_health.key" -maxdepth 0 -delete
if mqtt_allow_verified_active_secret_destination \
    "$active" "$active" "$active" test; then
  fail "an incomplete active directory was accepted"
fi
printf 'regression-mqtt_health.key\n' >"$active/mqtt_health.key"

printf 'unexpected\n' >"$active/unexpected"
if mqtt_allow_verified_active_secret_destination \
    "$active" "$active" "$active" test; then
  fail "an active directory with an unexpected entry was accepted"
fi
find "$active/unexpected" -maxdepth 0 -delete

if mqtt_allow_verified_active_secret_destination \
    "$active" "$active" "$active" production; then
  fail "test PKI was accepted for production"
fi
find "$active/.test_mqtt_pki" -maxdepth 0 -delete
mqtt_allow_verified_active_secret_destination \
  "$active" "$active" "$active" production || \
  fail "the exact verified active production directory was rejected"

echo "MQTT runtime-directory guard regression passed."
