#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${1:-}"
[[ -n "$image" ]] || { echo "Usage: $0 REPOSITORY:TAG" >&2; exit 2; }
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }

base_image="${FLEET_PYTHON_BASE_IMAGE:-}"
kubectl_binary="${FLEET_KUBECTL_BINARY:-}"
helm_binary="${FLEET_HELM_BINARY:-}"
kubectl_digest="${FLEET_KUBECTL_SHA256:-}"
helm_digest="${FLEET_HELM_SHA256:-}"

[[ "$base_image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "FLEET_PYTHON_BASE_IMAGE must be digest-pinned" >&2
  exit 1
}
[[ -x "$kubectl_binary" && -x "$helm_binary" ]] || {
  echo "FLEET_KUBECTL_BINARY and FLEET_HELM_BINARY must be executable files" >&2
  exit 1
}
[[ "$kubectl_digest" =~ ^[0-9a-f]{64}$ && "$helm_digest" =~ ^[0-9a-f]{64}$ ]] || {
  echo "FLEET_KUBECTL_SHA256 and FLEET_HELM_SHA256 are required" >&2
  exit 1
}
printf '%s  %s\n' "$kubectl_digest" "$kubectl_binary" | sha256sum -c -
printf '%s  %s\n' "$helm_digest" "$helm_binary" | sha256sum -c -

build_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

cp -a -- "$root_dir/src" "$root_dir/web" "$root_dir/config" \
  "$root_dir/schemas" "$root_dir/component_descriptors" "$root_dir/deploy" \
  "$root_dir/VERSION" "$root_dir/RELEASE_REVISION" "$build_dir/"
cp -- "$kubectl_binary" "$build_dir/kubectl"
cp -- "$helm_binary" "$build_dir/helm"
cp -- "$root_dir/deploy/images/automation-fleet/Dockerfile" "$build_dir/Dockerfile"

docker build \
  --build-arg "PYTHON_RUNTIME_IMAGE=$base_image" \
  --label "org.opencontainers.image.kubectl-sha256=$kubectl_digest" \
  --label "org.opencontainers.image.helm-sha256=$helm_digest" \
  --tag "$image" \
  "$build_dir"

echo "Built fleet image: $image"
