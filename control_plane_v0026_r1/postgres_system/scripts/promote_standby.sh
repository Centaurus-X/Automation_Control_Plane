#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

if [[ "${1:-}" != "--confirm-primary-is-fenced" ]]; then
  echo "Fence the old Primary, then rerun with --confirm-primary-is-fenced" >&2
  exit 64
fi

primary_id="$(docker compose ps -q postgres-primary)"
[[ -n "$primary_id" ]] || { echo "Primary container is unavailable." >&2; exit 1; }
docker compose stop postgres-primary
docker network disconnect automation_database "$primary_id"
docker compose exec -T -u postgres postgres-standby \
  pg_ctl -D /var/lib/postgresql/18/docker promote -w
standby_id="$(docker compose ps -q postgres-standby)"
[[ -n "$standby_id" ]] || { echo "Standby container is unavailable." >&2; exit 1; }
docker network disconnect automation_database "$standby_id"
docker network connect --alias automation-postgres automation_database "$standby_id"
docker compose exec -T -u postgres postgres-standby \
  psql -d automation_platform -Atqc "SELECT NOT pg_is_in_recovery();" | grep -Fx t >/dev/null
echo "Standby promoted and automation-postgres routed to the promoted database."
