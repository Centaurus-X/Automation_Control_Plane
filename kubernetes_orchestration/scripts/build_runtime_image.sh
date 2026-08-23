#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
archive=""
sidecar=""
image=""

while (($#)); do
  case "$1" in
    --archive) archive="${2:-}"; shift 2 ;;
    --sidecar) sidecar="${2:-}"; shift 2 ;;
    --image) image="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -f "$archive" && -f "$sidecar" && -n "$image" ]] || {
  echo "Usage: $0 --archive FILE --sidecar FILE --image REPOSITORY:TAG" >&2
  exit 2
}
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
command -v sha256sum >/dev/null 2>&1 || { echo "sha256sum is required" >&2; exit 69; }

expected="$(awk 'NR == 1 {print $1}' "$sidecar")"
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo "Invalid SHA-256 sidecar" >&2; exit 1; }
release_expected="c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
[[ "$expected" == "$release_expected" ]] || {
  echo "Runtime sidecar does not match the v0101 release binding" >&2
  exit 1
}
observed="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$observed" == "$expected" ]] || { echo "Runtime archive SHA-256 mismatch" >&2; exit 1; }

base_image="${AUTOMATION_RUNTIME_BASE_IMAGE:-}"
[[ "$base_image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "AUTOMATION_RUNTIME_BASE_IMAGE must be digest-pinned" >&2
  exit 1
}

PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/validate_runtime_archive.py" \
  "$archive" --expected-digest "$expected"

build_dir="$(mktemp -d)"
cleanup() {
  rm -rf -- "$build_dir"
}
trap cleanup EXIT

cp -- "$archive" "$build_dir/automation_system.zip"
cp -- "$root_dir/deploy/images/automation-system/Dockerfile" "$build_dir/Dockerfile"

docker build \
  --build-arg "PYTHON_RUNTIME_IMAGE=$base_image" \
  --build-arg "RUNTIME_ARCHIVE_SHA256=$expected" \
  --label "org.opencontainers.image.source-archive-sha256=$expected" \
  --tag "$image" \
  "$build_dir"

echo "Built runtime image: $image"
echo "Source archive SHA-256: $expected"
