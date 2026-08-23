#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

latest="$(docker compose exec -T -u postgres postgres-primary \
  find /backup -maxdepth 1 -type f -name 'automation_platform_*.dump' \
  -printf '%T@ %f\n' | sort -nr | awk 'NR==1 {print $2}')"
[[ -n "$latest" ]] || { echo "No logical backup found." >&2; exit 1; }
docker compose exec -T postgres-primary \
  sh -c "cd /backup && sha256sum -c '${latest}.sha256'"
test_db="automation_restore_test_$(date -u +%Y%m%d%H%M%S)"

docker compose exec -T -u postgres postgres-primary createdb "$test_db"
cleanup() {
  docker compose exec -T -u postgres postgres-primary dropdb --if-exists "$test_db" >/dev/null
}
trap cleanup EXIT
docker compose exec -T -u postgres postgres-primary \
  pg_restore --exit-on-error --no-owner --no-privileges \
  -d "$test_db" "/backup/$latest"
docker compose exec -T -u postgres postgres-primary \
  psql -d "$test_db" -Atqc "SELECT current_database();" | grep -Fx "$test_db" >/dev/null
echo "Isolated logical restore test passed."
