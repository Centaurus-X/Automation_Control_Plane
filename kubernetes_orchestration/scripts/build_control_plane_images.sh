#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
control_dir="$(cd "$root_dir/../control_plane_v0026_r1" && pwd)"
registry=""
master_base=""
proxy_base=""
mcp_base=""
platform="linux/amd64"
output="$PWD/control-plane-images.lock.json"

while (($#)); do
  case "$1" in
    --registry) registry="${2:-}"; shift 2 ;;
    --master-base) master_base="${2:-}"; shift 2 ;;
    --proxy-base) proxy_base="${2:-}"; shift 2 ;;
    --mcp-base) mcp_base="${2:-}"; shift 2 ;;
    --platform) platform="${2:-}"; shift 2 ;;
    --output) output="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ "$registry" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]+)?(/[A-Za-z0-9][A-Za-z0-9._-]*)*$ ]] || {
  echo "--registry is required and contains invalid characters" >&2
  exit 2
}
for reference in "$master_base" "$proxy_base" "$mcp_base"; do
  [[ "$reference" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
    echo "Every base image must be an immutable repository@sha256 reference" >&2
    exit 2
  }
done
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 69; }
python3 "$root_dir/tools/verify_release.py" --root "$root_dir/.." --quiet

build_and_push() {
  local reference="$1"
  local dockerfile="$2"
  local context="$3"
  local base="$4"
  docker buildx build \
    --platform "$platform" \
    --pull \
    --provenance=mode=max \
    --sbom=true \
    --build-arg "PYTHON_RUNTIME_IMAGE=$base" \
    --file "$dockerfile" \
    --tag "$reference" \
    --push \
    "$context"
}

manifest_digest() {
  docker buildx imagetools inspect "$1" --raw | sha256sum | awk '{print "sha256:" $1}'
}

master_ref="$registry/automation-master:0.26.0-v0027-r3"
proxy_ref="$registry/automation-proxy:0.26.0-v0027-r3"
mcp_ref="$registry/automation-mcp-controller:0.1.0-v0027-r3"

build_and_push "$master_ref" "$control_dir/master_node/Dockerfile" \
  "$control_dir/master_node" "$master_base"
build_and_push "$proxy_ref" "$root_dir/deploy/images/control-plane/proxy.Dockerfile" \
  "$control_dir/proxy_gateway" "$proxy_base"
build_and_push "$mcp_ref" "$root_dir/deploy/images/control-plane/mcp-controller.Dockerfile" \
  "$control_dir/mcp_controller_server" "$mcp_base"

master_digest="$(manifest_digest "$master_ref")"
proxy_digest="$(manifest_digest "$proxy_ref")"
mcp_digest="$(manifest_digest "$mcp_ref")"

install -d -m 0700 "$(dirname "$output")"
temporary="${output}.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT
printf '{\n' >"$temporary"
printf '  "schema_version": 1,\n' >>"$temporary"
printf '  "master": {"repository": "%s/automation-master", "digest": "%s"},\n' "$registry" "$master_digest" >>"$temporary"
printf '  "proxy": {"repository": "%s/automation-proxy", "digest": "%s"},\n' "$registry" "$proxy_digest" >>"$temporary"
printf '  "mcp-controller": {"repository": "%s/automation-mcp-controller", "digest": "%s"}\n' "$registry" "$mcp_digest" >>"$temporary"
printf '}\n' >>"$temporary"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$output"
trap - EXIT
echo "Immutable image lock written: $output"
