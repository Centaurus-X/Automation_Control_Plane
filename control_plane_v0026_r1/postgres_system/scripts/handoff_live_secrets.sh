#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

primary_id="$(docker compose ps -q postgres-primary)"
if [[ -z "$primary_id" ]]; then
  echo "No active PostgreSQL runtime material requires handoff."
  exit 0
fi
[[ "$(docker inspect --format '{{.State.Status}}' "$primary_id")" == "running" ]] || {
  echo "Active PostgreSQL Primary is not running." >&2
  exit 1
}

"$root_dir/scripts/verify_role_credentials.sh" "$primary_id"
"$root_dir/scripts/verify_replication.sh"

secret_dir="$root_dir/secrets"
[[ ! -L "$secret_dir" ]] || {
  echo "Refusing a symbolic-link PostgreSQL runtime-material directory." >&2
  exit 78
}
install -d -m 0700 "$secret_dir"

names=(
  postgres_superuser_password
  replication_password
  master_migrator_password
  master_app_password
  proxy_migrator_password
  proxy_app_password
)
declare -A sources=()
while IFS=$'\t' read -r destination source; do
  for name in "${names[@]}"; do
    if [[ "$destination" == "/run/secrets/$name" ]]; then
      sources["$name"]="$source"
    fi
  done
done < <(docker inspect --format '{{range .Mounts}}{{printf "%s\t%s\n" .Destination .Source}}{{end}}' "$primary_id")

for name in "${names[@]}"; do
  source="${sources[$name]:-}"
  [[ "$source" == /* && -f "$source" && ! -L "$source" ]] || {
    echo "Live PostgreSQL runtime material is missing or unsafe: $name" >&2
    exit 66
  }
  sources["$name"]="$(readlink -f -- "$source")"
  [[ -s "${sources[$name]}" ]] || {
    echo "Live PostgreSQL runtime material is empty: $name" >&2
    exit 66
  }
done

temporary=""
cleanup() {
  if [[ -n "$temporary" ]]; then
    rm -f -- "$temporary"
  fi
}
trap cleanup EXIT

for name in "${names[@]}"; do
  source="${sources[$name]}"
  target="$secret_dir/$name"
  [[ ! -L "$target" ]] || {
    echo "Refusing a symbolic-link PostgreSQL runtime-material target: $name" >&2
    exit 78
  }
  if [[ -f "$target" && "$(readlink -f -- "$target")" == "$source" ]]; then
    continue
  fi
  temporary="$(mktemp "$secret_dir/.handoff-${name}.XXXXXX")"
  install -m 0600 -- "$source" "$temporary"
  [[ -s "$temporary" ]]
  mv -f -- "$temporary" "$target"
  temporary=""
done

echo "PASS PostgreSQL runtime-material continuity handoff."
