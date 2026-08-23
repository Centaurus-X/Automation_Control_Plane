#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

export POSTGRES_BACKUP_HOST_UID="${POSTGRES_BACKUP_HOST_UID:-$(id -u)}"
[[ "$POSTGRES_BACKUP_HOST_UID" =~ ^[0-9]+$ ]] || {
  echo "POSTGRES_BACKUP_HOST_UID must be numeric." >&2
  exit 64
}

sentinel="recreate_$(date -u +%Y%m%d%H%M%S)_$RANDOM"
docker compose exec -T -u postgres postgres-primary psql -d automation_platform \
  -v ON_ERROR_STOP=1 -v sentinel="$sentinel" <<'SQL'
CREATE TABLE IF NOT EXISTS public.__container_recreate_test(value text PRIMARY KEY);
INSERT INTO public.__container_recreate_test(value) VALUES (:'sentinel');
SQL

docker compose up -d --force-recreate postgres-primary

for _attempt in $(seq 1 48); do
  if docker compose exec -T -u postgres postgres-primary \
      pg_isready -h 127.0.0.1 -U postgres -d automation_platform >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

found="$(docker compose exec -T -u postgres postgres-primary psql \
  -d automation_platform -Atqc \
  "SELECT count(*) FROM public.__container_recreate_test WHERE value='$sentinel';")"
[[ "$found" == "1" ]] || { echo "Persistent sentinel was lost" >&2; exit 1; }
docker compose exec -T -u postgres postgres-primary psql -d automation_platform \
  -v ON_ERROR_STOP=1 -v sentinel="$sentinel" \
  -c "DELETE FROM public.__container_recreate_test WHERE value='$sentinel';"
echo "Primary data survived a forced container recreate."
