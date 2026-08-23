#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker compose version >/dev/null
"$root_dir/setup.sh"
if ! docker network inspect automation_control >/dev/null 2>&1; then
  docker network create --driver bridge automation_control >/dev/null
fi
[[ "$(docker network inspect --format '{{.Driver}}' automation_control)" == "bridge" ]] || {
  echo "automation_control must use the bridge driver." >&2
  exit 78
}
docker network inspect automation_database >/dev/null 2>&1 || {
  echo "Deploy postgres_system before Master." >&2
  exit 69
}
docker compose config --quiet
if [[ "${CONTROL_PLANE_IMAGES_PREPARED:-0}" != "1" ]]; then
  python_runtime_image="${PYTHON_RUNTIME_IMAGE:-automation-python-free-threaded:3.14.6-v14}"
  if ! docker image inspect "$python_runtime_image" >/dev/null 2>&1; then
    docker build --pull --tag "$python_runtime_image" "$root_dir/../runtime/python314t"
  fi
  docker compose build
fi
"$root_dir/scripts/prepare_compose_secrets.sh" \
  "$root_dir/secrets" 65532 "${MASTER_IMAGE:-automation-master-node:0.26.0}"
docker compose run --rm master-migrate
docker compose up -d --force-recreate --remove-orphans master-node
echo "Master-Node deployment started."
