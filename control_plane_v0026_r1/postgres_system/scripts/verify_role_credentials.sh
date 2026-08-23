#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"
container_id="${1:-$(docker compose ps -q postgres-primary)}"
[[ -n "$container_id" ]] || {
  echo "PostgreSQL Primary is unavailable for role authentication checks." >&2
  exit 1
}
[[ "$(docker inspect --format '{{.State.Status}}' "$container_id")" == "running" ]] || {
  echo "PostgreSQL Primary is not running for role authentication checks." >&2
  exit 1
}

docker exec -i -u postgres "$container_id" bash -s <<'INNER'
set -Eeuo pipefail
passfile=""
cleanup() {
  if [[ -n "$passfile" ]]; then
    rm -f -- "$passfile"
  fi
}
trap cleanup EXIT

verify_login() {
  local role="$1"
  local database="$2"
  local secret_path="$3"
  local value escaped
  [[ -s "$secret_path" ]] || {
    echo "Required PostgreSQL role material is missing: $role" >&2
    exit 66
  }
  value="$(tr -d '\r\n' < "$secret_path")"
  [[ -n "$value" ]] || {
    echo "Required PostgreSQL role material is empty: $role" >&2
    exit 66
  }
  escaped="${value//\\/\\\\}"
  escaped="${escaped//:/\\:}"
  passfile="$(mktemp /tmp/role-auth.XXXXXX)"
  chmod 0600 "$passfile"
  printf '127.0.0.1:5432:%s:%s:%s\n' "$database" "$role" "$escaped" > "$passfile"
  PGCONNECT_TIMEOUT=5 PGPASSFILE="$passfile" psql \
    "host=127.0.0.1 port=5432 dbname=$database user=$role sslmode=verify-ca sslrootcert=/var/lib/postgresql/tls/ca.crt" \
    -Atqc 'SELECT 1' >/dev/null
  rm -f -- "$passfile"
  passfile=""
}

verify_login postgres postgres /run/secrets/postgres_superuser_password
verify_login master_migrator automation_platform /run/secrets/master_migrator_password
verify_login master_app automation_platform /run/secrets/master_app_password
verify_login proxy_migrator automation_platform /run/secrets/proxy_migrator_password
verify_login proxy_app automation_platform /run/secrets/proxy_app_password
INNER

echo "PASS PostgreSQL role authentication matches mounted runtime material."
