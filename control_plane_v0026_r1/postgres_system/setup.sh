#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker compose version >/dev/null

export POSTGRES_BACKUP_HOST_UID="${POSTGRES_BACKUP_HOST_UID:-$(id -u)}"
[[ "$POSTGRES_BACKUP_HOST_UID" =~ ^[0-9]+$ ]] || {
  echo "POSTGRES_BACKUP_HOST_UID must be numeric." >&2
  exit 64
}

if [[ -L "$root_dir/backup" ]]; then
  echo "Refusing a symbolic-link PostgreSQL backup directory." >&2
  exit 78
fi
if [[ ! -d "$root_dir/backup" ]]; then
  install -d -m 0750 "$root_dir/backup"
fi

"$root_dir/scripts/generate_secrets.sh"
if [[ -n "${POSTGRES_CA_SOURCE:-}" && -n "${POSTGRES_PRIMARY_CERT_SOURCE:-}" \
      && -n "${POSTGRES_PRIMARY_KEY_SOURCE:-}" && -n "${POSTGRES_STANDBY_CERT_SOURCE:-}" \
      && -n "${POSTGRES_STANDBY_KEY_SOURCE:-}" ]]; then
  install -d -m 0700 "$root_dir/tls"
  install -m 0644 "$POSTGRES_CA_SOURCE" "$root_dir/tls/ca.crt"
  install -m 0644 "$POSTGRES_PRIMARY_CERT_SOURCE" "$root_dir/tls/primary.crt"
  install -m 0600 "$POSTGRES_PRIMARY_KEY_SOURCE" "$root_dir/tls/primary.key"
  install -m 0644 "$POSTGRES_STANDBY_CERT_SOURCE" "$root_dir/tls/standby.crt"
  install -m 0600 "$POSTGRES_STANDBY_KEY_SOURCE" "$root_dir/tls/standby.key"
  rm -f "$root_dir/tls/.test_tls_material"
elif [[ "${ALLOW_TEST_POSTGRES_TLS:-0}" == "1" ]]; then
  "$root_dir/scripts/generate_test_tls.sh"
elif [[ ! -s "$root_dir/tls/ca.crt" || ! -s "$root_dir/tls/primary.crt" \
        || ! -s "$root_dir/tls/primary.key" || ! -s "$root_dir/tls/standby.crt" \
        || ! -s "$root_dir/tls/standby.key" ]]; then
  echo "Production PostgreSQL TLS material is required." >&2
  exit 66
fi
if [[ -e "$root_dir/tls/.test_tls_material" \
      && "${ALLOW_TEST_POSTGRES_TLS:-0}" != "1" ]]; then
  echo "Refusing test PostgreSQL TLS material in production mode." >&2
  exit 78
fi
openssl verify -CAfile "$root_dir/tls/ca.crt" \
  "$root_dir/tls/primary.crt" "$root_dir/tls/standby.crt"
openssl x509 -checkhost automation-postgres -noout -in "$root_dir/tls/primary.crt"
openssl x509 -checkhost postgres-primary -noout -in "$root_dir/tls/primary.crt"
openssl x509 -checkhost postgres-standby -noout -in "$root_dir/tls/standby.crt"
for name in primary standby; do
  openssl x509 -checkend 86400 -noout \
    -in "$root_dir/tls/$name.crt" >/dev/null || {
    echo "PostgreSQL $name certificate expires within 24 hours." >&2
    exit 78
  }
  cert_public="$(openssl x509 -in "$root_dir/tls/$name.crt" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  key_public="$(openssl pkey -in "$root_dir/tls/$name.key" \
    -pubout -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  [[ -n "$cert_public" && "$cert_public" == "$key_public" ]] || {
    echo "PostgreSQL $name certificate/private-key mismatch." >&2
    exit 78
  }
done

if ! docker network inspect automation_database >/dev/null 2>&1; then
  docker network create --driver bridge --internal --subnet 172.30.0.0/24 \
    automation_database >/dev/null
fi
network_internal="$(docker network inspect --format '{{.Internal}}' automation_database)"
network_subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' automation_database)"
[[ "$network_internal" == "true" && "$network_subnet" == "172.30.0.0/24" ]] || {
  echo "automation_database must be internal with subnet 172.30.0.0/24." >&2
  exit 78
}

docker compose config --quiet
if [[ "${CONTROL_PLANE_IMAGES_PREPARED:-0}" != "1" ]]; then
  docker compose build --pull
fi
"$root_dir/scripts/prepare_compose_secrets.sh" \
  "$root_dir/secrets" 999 "${POSTGRES_IMAGE:-automation-postgres:18.4-v14}"
docker compose up -d --force-recreate --remove-orphans postgres-primary

for _attempt in $(seq 1 48); do
  primary_id="$(docker compose ps -q postgres-primary)"
  health="$(docker inspect --format '{{.State.Health.Status}}' "$primary_id" 2>/dev/null || true)"
  if [[ "$health" == "healthy" ]]; then
    break
  fi
  sleep 2
done
[[ "${health:-}" == "healthy" ]] || {
  echo "PostgreSQL Primary did not become healthy." >&2
  docker compose logs --tail=100 postgres-primary >&2 || true
  exit 1
}

docker compose up -d --force-recreate postgres-standby
"$root_dir/scripts/verify_replication.sh"
"$root_dir/scripts/verify_role_credentials.sh"
echo "PostgreSQL Primary and Standby are ready."
