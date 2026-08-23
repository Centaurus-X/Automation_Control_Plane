#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$root_dir/config"
runtime_dir="$root_dir/runtime-config"
helper_image="${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
owner_uid="${MQTT_CONFIG_OWNER_UID:-$(id -u)}"

[[ "$owner_uid" =~ ^[0-9]+$ ]] || {
  echo "MQTT_CONFIG_OWNER_UID must be numeric." >&2
  exit 64
}
for name in mosquitto.conf acl.conf; do
  [[ -f "$source_dir/$name" && ! -L "$source_dir/$name" ]] || {
    echo "MQTT source configuration is missing or unsafe: $source_dir/$name" >&2
    exit 66
  }
done
docker image inspect "$helper_image" >/dev/null 2>&1 || {
  echo "MQTT configuration helper image is unavailable: $helper_image" >&2
  exit 69
}

install -d -m 0750 "$runtime_dir"
docker run --rm \
  --network none \
  --read-only \
  --user 0:0 \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add FOWNER \
  --cap-add DAC_OVERRIDE \
  --entrypoint /bin/sh \
  --volume "$source_dir:/source:ro" \
  --volume "$runtime_dir:/target" \
  "$helper_image" \
  -eu -c '
    owner_uid="$1"
    for entry in /target/* /target/.[!.]*; do
      [ -e "$entry" ] || [ -L "$entry" ] || continue
      case "${entry##*/}" in
        mosquitto.conf|acl.conf) rm -f "$entry" ;;
        *) echo "Refusing unknown MQTT runtime-config entry: $entry" >&2; exit 78 ;;
      esac
    done
    cp /source/mosquitto.conf /target/mosquitto.conf
    cp /source/acl.conf /target/acl.conf
    chown 1883:1883 /target/mosquitto.conf /target/acl.conf
    chmod 0600 /target/mosquitto.conf
    chmod 0700 /target/acl.conf
    chown "${owner_uid}:1883" /target
    chmod 0750 /target
  ' mqtt-runtime-config "$owner_uid"

[[ "$(stat -c '%u:%g:%a' "$runtime_dir")" == "$owner_uid:1883:750" ]]
[[ "$(stat -c '%u:%g:%a' "$runtime_dir/mosquitto.conf")" == "1883:1883:600" ]]
[[ "$(stat -c '%u:%g:%a' "$runtime_dir/acl.conf")" == "1883:1883:700" ]]
echo "MQTT runtime configuration ownership and permissions are valid."
