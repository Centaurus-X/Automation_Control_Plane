#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
bash -n ./*.sh scripts/*.sh init/*.sh
grep -F "FROM postgres:18.4-bookworm" Dockerfile >/dev/null
grep -F "PGDATA: /var/lib/postgresql/18/docker" compose.yaml >/dev/null
grep -F "internal --subnet 172.30.0.0/24" setup.sh >/dev/null
grep -F "hostnossl all" config/pg_hba.conf >/dev/null
grep -F "ssl = on" config/postgresql-primary.conf >/dev/null
grep -F "wal_level = replica" config/postgresql-primary.conf >/dev/null
grep -F 'install -d -m 2770 -o "$backup_host_uid" -g postgres /backup' \
  scripts/container-entrypoint.sh >/dev/null
grep -F 'install -d -m 0700 -o postgres -g postgres /archive' \
  scripts/container-entrypoint.sh >/dev/null
grep -F 'sslmode=verify-full' scripts/container-entrypoint.sh >/dev/null
grep -F 'prepare_compose_secrets.sh' setup.sh >/dev/null
grep -F 'POSTGRES_BACKUP_HOST_UID' compose.yaml >/dev/null
grep -F 'automation-postgres:18.4-v14' compose.yaml >/dev/null
if command -v docker >/dev/null 2>&1; then
  docker compose config --quiet
fi
echo "PostgreSQL static deployment checks passed."
