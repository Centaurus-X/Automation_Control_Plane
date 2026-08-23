#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
profile="$1"

fail() {
  echo "PostgreSQL live-TLS handoff failed: $1" >&2
  exit 78
}

[[ "$profile" == "test" || "$profile" == "production" ]] ||
  fail "expected deployment profile test or production"
command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"

primary_ids="$(docker ps -q \
  --filter "label=com.docker.compose.project=automation-postgres" \
  --filter "label=com.docker.compose.service=postgres-primary")"
primary_count="$(printf '%s\n' "$primary_ids" | sed '/^$/d' | wc -l)"
[[ "$primary_count" -le 1 ]] ||
  fail "multiple active PostgreSQL Primary containers"
if [[ -z "$primary_ids" ]]; then
  echo "No active PostgreSQL TLS domain requires handoff."
  exit 0
fi

observed="$(docker inspect --format \
  '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
  "$primary_ids")"
[[ "$observed" == "running|healthy" ]] ||
  fail "PostgreSQL Primary is not healthy: $observed"

source_dir="$(docker inspect --format \
  '{{range .Mounts}}{{if eq .Destination "/run/automation-tls"}}{{println .Source}}{{end}}{{end}}' \
  "$primary_ids")"
[[ "$source_dir" = /* && -d "$source_dir" && ! -L "$source_dir" ]] ||
  fail "active PostgreSQL TLS mount source is unavailable or unsafe"
source_dir="$(readlink -f -- "$source_dir")"

for name in ca.crt primary.crt primary.key standby.crt standby.key; do
  path="$source_dir/$name"
  [[ -f "$path" && ! -L "$path" && -s "$path" ]] ||
    fail "active PostgreSQL TLS file is unavailable or unsafe: $name"
done

openssl verify -CAfile "$source_dir/ca.crt" \
  "$source_dir/primary.crt" "$source_dir/standby.crt" >/dev/null ||
  fail "certificate-chain verification failed"
for name in primary standby; do
  openssl x509 -checkend 86400 -noout -in "$source_dir/$name.crt" >/dev/null ||
    fail "$name certificate expires within 24 hours"
  cert_public="$(openssl x509 -in "$source_dir/$name.crt" -pubkey -noout \
    | openssl pkey -pubin -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  key_public="$(openssl pkey -in "$source_dir/$name.key" \
    -pubout -outform DER 2>/dev/null \
    | sha256sum | cut -d' ' -f1)"
  [[ -n "$cert_public" && "$cert_public" == "$key_public" ]] ||
    fail "$name certificate/private-key mismatch"
done
openssl x509 -checkhost automation-postgres -noout \
  -in "$source_dir/primary.crt" >/dev/null ||
  fail "Primary certificate does not cover automation-postgres"
openssl x509 -checkhost postgres-primary -noout \
  -in "$source_dir/primary.crt" >/dev/null ||
  fail "Primary certificate does not cover postgres-primary"
openssl x509 -checkhost postgres-standby -noout \
  -in "$source_dir/standby.crt" >/dev/null ||
  fail "Standby certificate does not cover postgres-standby"

ca_subject="$(openssl x509 -noout -subject -in "$source_dir/ca.crt")"
if [[ "$profile" == "production" ]] &&
   { [[ -e "$source_dir/.test_tls_material" ]] ||
     grep -Fi 'Test Database CA' <<<"$ca_subject" >/dev/null; }; then
  fail "test PostgreSQL TLS cannot be promoted into production"
fi

stage="$(mktemp -d "$root_dir/.postgres-tls-handoff.XXXXXX")"
previous=""
cleanup() {
  for directory in "$stage" "$previous"; do
    if [[ -n "$directory" && -d "$directory" &&
          "$directory" == "$root_dir"/.*tls-handoff.* ]]; then
      find "$directory" -depth -delete
    fi
  done
}
trap cleanup EXIT

stage_dir="$stage/runtime-tls-handoff"
install -d -m 0700 "$stage_dir"
install -m 0644 "$source_dir/ca.crt" "$stage_dir/ca.crt"
install -m 0644 "$source_dir/primary.crt" "$stage_dir/primary.crt"
install -m 0600 "$source_dir/primary.key" "$stage_dir/primary.key"
install -m 0644 "$source_dir/standby.crt" "$stage_dir/standby.crt"
install -m 0600 "$source_dir/standby.key" "$stage_dir/standby.key"
printf '%s\n' "$profile" >"$stage_dir/.live_postgres_tls_handoff"
chmod 0600 "$stage_dir/.live_postgres_tls_handoff"

destination="$root_dir/runtime-tls-handoff"
[[ ! -L "$destination" ]] ||
  fail "refusing symbolic-link PostgreSQL TLS handoff target"
if [[ -d "$destination" &&
      ! -e "$destination/.live_postgres_tls_handoff" &&
      -n "$(find "$destination" -mindepth 1 -print -quit)" ]]; then
  fail "refusing to overwrite unmarked PostgreSQL TLS handoff"
fi
if [[ -d "$destination" ]]; then
  previous="$root_dir/.runtime-tls-handoff.previous.$$"
  [[ ! -e "$previous" ]] ||
    fail "temporary PostgreSQL TLS handoff target already exists"
  mv -- "$destination" "$previous"
fi
mv -- "$stage_dir" "$destination"

echo "Active PostgreSQL TLS domain was verified and handed off for $profile rollout."
