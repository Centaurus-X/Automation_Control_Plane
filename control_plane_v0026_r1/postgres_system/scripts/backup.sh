#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
logical="/backup/automation_platform_${timestamp}.dump"
physical="/backup/base_${timestamp}"
backup_host_uid="${POSTGRES_BACKUP_HOST_UID:-$(id -u)}"
[[ "$backup_host_uid" =~ ^[0-9]+$ ]] || {
  echo "POSTGRES_BACKUP_HOST_UID must be numeric." >&2
  exit 64
}

container_id="$(docker compose ps -q postgres-primary)"
[[ -n "$container_id" ]] || {
  echo "PostgreSQL primary container is unavailable." >&2
  exit 1
}
live_backup_host_dir="$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/backup"}}{{.Source}}{{end}}{{end}}' \
  "$container_id")"
[[ "$live_backup_host_dir" = /* && -d "$live_backup_host_dir" &&
   ! -L "$live_backup_host_dir" ]] || {
  echo "Live PostgreSQL /backup bind source is unavailable or unsafe." >&2
  exit 1
}

docker compose exec -T -u postgres postgres-primary \
  pg_dump -Fc -d automation_platform -f "$logical"
docker compose exec -T -u postgres postgres-primary \
  pg_basebackup -D "$physical" -Fp -Xs -P --checkpoint=fast
docker compose exec -T postgres-primary \
  sh -c "sha256sum '$logical' > '${logical}.sha256' && pg_verifybackup '$physical'"

# Container-created files otherwise remain owned by UID 999. Preserve a
# writable postgres group while making completed backups readable by the host
# deployment user for restore discovery and off-host transfer.
docker compose exec -T -u root postgres-primary \
  sh -eu -c '
    owner_uid="$1"
    chown -R "${owner_uid}:postgres" /backup
    chmod 2770 /backup
    find /backup -mindepth 1 -type d -exec chmod 0750 {} +
    find /backup -mindepth 1 -type f -exec chmod 0640 {} +
  ' backup-permissions "$backup_host_uid"

[[ -r "$live_backup_host_dir/automation_platform_${timestamp}.dump" ]] || {
  echo "Completed logical backup is not readable by host UID $backup_host_uid." >&2
  exit 1
}

echo "Backup completed under $live_backup_host_dir with timestamp $timestamp."
echo "Copy backup/ and WAL archive data to an independent off-host target."
