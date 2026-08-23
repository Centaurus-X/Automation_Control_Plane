#!/usr/bin/env bash

set -Eeuo pipefail

role="${POSTGRES_NODE_ROLE:-primary}"
pgdata="${PGDATA:-/var/lib/postgresql/18/docker}"
backup_host_uid="${POSTGRES_BACKUP_HOST_UID:-999}"
[[ "$backup_host_uid" =~ ^[0-9]+$ ]] || {
  echo "POSTGRES_BACKUP_HOST_UID must be numeric." >&2
  exit 64
}

prepare_runtime_directories() {
  # /backup is a host bind mount. Keep the deployment user as owner while the
  # postgres group retains write access, including after a container recreate.
  install -d -m 2770 -o "$backup_host_uid" -g postgres /backup
  if [[ "$role" == "primary" ]]; then
    install -d -m 0700 -o postgres -g postgres /archive
  fi
}

install_tls() {
  local prefix="$1"
  install -d -m 0700 -o postgres -g postgres /var/lib/postgresql/tls
  install -m 0644 -o postgres -g postgres /run/automation-tls/ca.crt \
    /var/lib/postgresql/tls/ca.crt
  install -m 0644 -o postgres -g postgres "/run/automation-tls/${prefix}.crt" \
    "/var/lib/postgresql/tls/${prefix}.crt"
  install -m 0600 -o postgres -g postgres "/run/automation-tls/${prefix}.key" \
    "/var/lib/postgresql/tls/${prefix}.key"
}

initialize_standby() {
  if [[ -s "$pgdata/PG_VERSION" ]]; then
    return
  fi
  install -d -m 0700 -o postgres -g postgres "$pgdata"
  install -m 0600 -o postgres -g postgres /dev/null /var/lib/postgresql/.pgpass
  printf '%s:%s:*:replicator:%s\n' \
    "${PRIMARY_HOST:-postgres-primary}" "${PRIMARY_PORT:-5432}" \
    "$(< /run/secrets/replication_password)" > /var/lib/postgresql/.pgpass
  chown postgres:postgres /var/lib/postgresql/.pgpass
  until pg_isready -h "${PRIMARY_HOST:-postgres-primary}" \
      -p "${PRIMARY_PORT:-5432}" -U replicator >/dev/null 2>&1; do
    sleep 2
  done
  local primary_conninfo
  primary_conninfo="host=${PRIMARY_HOST:-postgres-primary} port=${PRIMARY_PORT:-5432} "
  primary_conninfo+="user=replicator sslmode=verify-full "
  primary_conninfo+="sslrootcert=/var/lib/postgresql/tls/ca.crt"
  gosu postgres env PGPASSFILE=/var/lib/postgresql/.pgpass pg_basebackup \
    -d "$primary_conninfo" \
    -D "$pgdata" \
    -Fp -Xs -P -R --slot=automation_standby --checkpoint=fast
  printf "primary_conninfo = 'host=%s port=%s user=replicator passfile=/var/lib/postgresql/.pgpass sslmode=verify-full sslrootcert=/var/lib/postgresql/tls/ca.crt'\n" \
    "${PRIMARY_HOST:-postgres-primary}" "${PRIMARY_PORT:-5432}" \
    >> "$pgdata/postgresql.auto.conf"
  chown postgres:postgres "$pgdata/postgresql.auto.conf"
}

prepare_runtime_directories

if [[ "$role" == "primary" ]]; then
  install_tls primary
elif [[ "$role" == "standby" ]]; then
  install_tls standby
  initialize_standby
else
  echo "Unsupported POSTGRES_NODE_ROLE: $role" >&2
  exit 64
fi

exec /usr/local/bin/docker-entrypoint.sh "$@"
