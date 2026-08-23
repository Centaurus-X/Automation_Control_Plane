#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# >= 1 )); then
  source_root="$1"
else
  source_root="${CONTROL_PLANE_STATE_SOURCE:-}"
fi
target_root="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
database_volume="automation-postgres_postgres_primary_data"

critical_state=(
  postgres_system/secrets/postgres_superuser_password
  postgres_system/secrets/replication_password
  postgres_system/secrets/master_migrator_password
  postgres_system/secrets/master_app_password
  postgres_system/secrets/proxy_migrator_password
  postgres_system/secrets/proxy_app_password
  proxy_gateway/secrets/password_pepper
  proxy_gateway/secrets/token_pepper
  proxy_gateway/secrets/binding_pepper
)

state_directories=(
  mqtt_broker/test-pki
  mqtt_broker/secrets
  postgres_system/secrets
  postgres_system/tls
  postgres_system/test_ca_private
  master_node/secrets
  proxy_gateway/secrets
)

state_is_complete() {
  local root="$1"
  local relative
  for relative in "${critical_state[@]}"; do
    [[ -s "$root/$relative" ]] || return 1
  done
}

database_volume_exists() {
  docker volume inspect "$database_volume" >/dev/null 2>&1
}

if state_is_complete "$target_root"; then
  if [[ -n "$source_root" && -d "$source_root" ]] \
      && state_is_complete "$source_root"; then
    for relative in "${critical_state[@]}"; do
      cmp "$source_root/$relative" "$target_root/$relative" >/dev/null || {
        echo "Target runtime credentials conflict with the requested upgrade source." >&2
        echo "Conflicting file: $relative" >&2
        exit 78
      }
    done
  fi
  echo "Existing runtime credentials in the target release are reused."
  exit 0
fi

if [[ -z "$source_root" || ! -d "$source_root" ]]; then
  if database_volume_exists; then
    echo "Persistent PostgreSQL data exists, but matching runtime credentials are missing." >&2
    echo "Set CONTROL_PLANE_STATE_SOURCE to the previous release directory." >&2
    exit 66
  fi
  echo "No previous runtime state found; a fresh installation will be initialized."
  exit 0
fi

source_root="$(cd "$source_root" && pwd -P)"
target_root="$(cd "$target_root" && pwd -P)"
[[ "$source_root" != "$target_root" ]] || {
  echo "Runtime state source and target must be different directories." >&2
  exit 64
}
if ! state_is_complete "$source_root"; then
  if database_volume_exists; then
    echo "Previous release state is incomplete: $source_root" >&2
    exit 66
  fi
  echo "Incomplete previous state was ignored because no database volume exists."
  exit 0
fi

for relative in "${state_directories[@]}"; do
  source_path="$source_root/$relative"
  target_path="$target_root/$relative"
  [[ -d "$source_path" ]] || continue
  unsafe_entry="$(find "$source_path" -mindepth 1 \
    ! -type f ! -type d -print -quit)"
  [[ -z "$unsafe_entry" ]] || {
    echo "Runtime state contains an unsupported file type: $unsafe_entry" >&2
    exit 65
  }
  if [[ -d "$target_path" \
      && -n "$(find "$target_path" -mindepth 1 -print -quit)" ]]; then
    diff -qr "$source_path" "$target_path" >/dev/null || {
      echo "Refusing to overwrite conflicting runtime state: $target_path" >&2
      exit 78
    }
    continue
  fi
  install -d -m 0700 "$target_path"
  # Compose preparation may have assigned a container runtime GID in the
  # previous release. The unprivileged deployment user must own the imported
  # copy; package deploy helpers restore the required runtime GID afterwards.
  cp -a --no-preserve=ownership "$source_path/." "$target_path/"
done

state_is_complete "$target_root" || {
  echo "Runtime-state import did not produce a complete credential set." >&2
  exit 66
}
echo "Runtime credentials, PKI and test TLS state were imported from $source_root."
