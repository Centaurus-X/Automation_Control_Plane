#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
python_runtime_image="${PYTHON_RUNTIME_IMAGE:-automation-python-free-threaded:3.14.6-v14}"
if ! docker image inspect "$python_runtime_image" >/dev/null 2>&1; then
  docker build --pull --tag "$python_runtime_image" "$root_dir/../runtime/python314t"
fi
docker build --build-arg "PYTHON_RUNTIME_IMAGE=$python_runtime_image" \
  --tag automation-master-node:test .
echo "Master release-image tests passed."
