#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

persistence="$root_dir/postgres_system/scripts/test_persistence.sh"
backup="$root_dir/postgres_system/scripts/backup.sh"
restore="$root_dir/postgres_system/scripts/restore_test.sh"
entrypoint="$root_dir/postgres_system/scripts/container-entrypoint.sh"
postgres_setup="$root_dir/postgres_system/setup.sh"
postgres_compose="$root_dir/postgres_system/compose.yaml"
postgres_handoff="$root_dir/postgres_system/scripts/handoff_live_secrets.sh"
postgres_role_auth="$root_dir/postgres_system/scripts/verify_role_credentials.sh"
mqtt_config="$root_dir/mqtt_broker/config/mosquitto.conf"
mqtt_compose="$root_dir/mqtt_broker/compose.yaml"
mqtt_prepare="$root_dir/mqtt_broker/scripts/prepare_runtime_config.sh"
proxy_main="$root_dir/proxy_gateway/proxy_gateway/src/gateway_main.py"

bash -n "$persistence" "$backup" "$restore" "$entrypoint" \
  "$postgres_setup" "$postgres_handoff" "$postgres_role_auth" "$mqtt_prepare"

grep -F "value='\$sentinel'" "$persistence" >/dev/null
grep -F 'POSTGRES_BACKUP_HOST_UID="${POSTGRES_BACKUP_HOST_UID:-$(id -u)}"' \
  "$persistence" >/dev/null
if grep -F "value=:'sentinel'" "$persistence" >/dev/null; then
  echo "The broken psql variable expression is still present." >&2
  exit 1
fi

grep -F 'POSTGRES_BACKUP_HOST_UID' "$postgres_compose" >/dev/null
grep -F 'if [[ ! -d "$root_dir/backup" ]]; then' "$postgres_setup" >/dev/null
grep -F 'install -d -m 2770 -o "$backup_host_uid" -g postgres /backup' \
  "$entrypoint" >/dev/null
grep -F 'chown -R "${owner_uid}:postgres" /backup' "$backup" >/dev/null
grep -F 'live_backup_host_dir="$(docker inspect --format ' "$backup" >/dev/null
grep -F '[[ -r "$live_backup_host_dir/automation_platform_${timestamp}.dump" ]]' \
  "$backup" >/dev/null
if grep -F '"$root_dir/backup/automation_platform_${timestamp}.dump"' \
    "$backup" >/dev/null; then
  echo "Backup verification is still coupled to the target release path." >&2
  exit 1
fi
grep -F 'Completed logical backup is not readable' "$backup" >/dev/null
grep -F 'handoff_live_secrets.sh' "$root_dir/administration/component_adapter.py" >/dev/null
grep -F 'verify_role_credentials.sh' "$postgres_setup" >/dev/null
grep -F 'verify_role_credentials.sh' "$root_dir/postgres_system/status.sh" >/dev/null
grep -F '/run/secrets/$name' "$postgres_handoff" >/dev/null
grep -F 'PGPASSFILE="$passfile" psql' "$postgres_role_auth" >/dev/null
grep -F 'sslrootcert=/var/lib/postgresql/tls/ca.crt' "$postgres_role_auth" >/dev/null
grep -F 'docker compose exec -T -u postgres postgres-primary' "$restore" >/dev/null
grep -F 'find /backup' "$restore" >/dev/null
if grep -F 'find backup' "$restore" >/dev/null; then
  echo "Restore discovery must not traverse the bind mount as the host user." >&2
  exit 1
fi

grep -F 'listener_allow_anonymous false' "$mqtt_config" >/dev/null
if grep -E '^(per_listener_settings|message_size_limit)[[:space:]]' \
    "$mqtt_config" >/dev/null; then
  echo "Deprecated Mosquitto 2.1 directives remain active." >&2
  exit 1
fi
grep -F './runtime-config:/mosquitto/config:ro' "$mqtt_compose" >/dev/null
grep -F 'chmod 0700 /target/acl.conf' "$mqtt_prepare" >/dev/null
grep -F 'chown 1883:1883 /target/mosquitto.conf /target/acl.conf' \
  "$mqtt_prepare" >/dev/null

grep -F 'interface="asgi3"' "$proxy_main" >/dev/null
grep -F 'ws="none"' "$proxy_main" >/dev/null
grep -F 'runtime["wss"]["serve"]' "$proxy_main" >/dev/null
if grep -F 'websockets-sansio' "$proxy_main" >/dev/null; then
  echo "Uvicorn WSS support was reintroduced." >&2
  exit 1
fi

echo "PASS extended recovery permissions, Mosquitto 2.1 and split WSS/HTTPS regressions."
