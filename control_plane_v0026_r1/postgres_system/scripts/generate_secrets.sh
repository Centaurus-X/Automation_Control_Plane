#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/secrets"
install -d -m 0700 "$secret_dir"

generate_one() {
  local name="$1"
  local path="$secret_dir/$name"
  if [[ -s "$path" ]]; then
    chmod 0600 "$path"
    return
  fi
  umask 077
  openssl rand -base64 48 > "$path"
  chmod 0600 "$path"
}

for name in \
  postgres_superuser_password \
  replication_password \
  master_migrator_password \
  master_app_password \
  proxy_migrator_password \
  proxy_app_password; do
  generate_one "$name"
done

echo "Database secret files are ready."
