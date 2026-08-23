#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
(( $# > 0 )) || {
  echo "Usage: $0 CONTAINER_COMMAND [ARGUMENT ...]" >&2
  exit 64
}

if [[ "${CONTROL_PLANE_VALIDATION_ACTIVE:-}" == "/release" ]]; then
  exec "$@"
fi

command -v docker >/dev/null 2>&1 || {
  echo "BLOCKED container validation: docker is required." >&2
  exit 69
}
docker info >/dev/null

case "$root_dir" in
  *','*|*$'\n'*)
    echo "BLOCKED container validation: release path contains an unsafe mount character." >&2
    exit 64
    ;;
esac

image="${CONTROL_PLANE_VALIDATION_IMAGE:-automation-control-plane-validation:0.26.0}"
fingerprint="$({
  sha256sum \
    "$root_dir/.dockerignore" \
    "$root_dir/runtime/validation/Dockerfile" \
    "$root_dir/requirements-validation.txt" \
    "$root_dir/master_node/requirements.txt" \
    "$root_dir/proxy_gateway/requirements.txt" \
    "$root_dir/integration_test/e2e_v0025/requirements-e2e.txt" \
    "$root_dir/proxy_gateway/tools/check_python_dependencies.py"
} | sha256sum | awk '{print $1}')"
installed_fingerprint="$(docker image inspect \
  --format '{{with .Config.Labels}}{{index . "org.opencontainers.image.validation-fingerprint"}}{{end}}' \
  "$image" 2>/dev/null || true)"

if [[ "$installed_fingerprint" != "$fingerprint" ]]; then
  echo "Building pinned v0026 validation image (no host venv, no host pip)."
  docker build --pull --no-cache \
    --build-arg "VALIDATION_FINGERPRINT=$fingerprint" \
    --file "$root_dir/runtime/validation/Dockerfile" \
    --tag "$image" \
    "$root_dir"
fi

docker_options=(
  --rm
  --init
  --network host
  --read-only
  --user "$(id -u):$(id -g)"
  --env HOME=/tmp
  --env PYTHONDONTWRITEBYTECODE=1
  --env PYTHONNOUSERSITE=1
  --env CONTROL_PLANE_VALIDATION_ACTIVE=/release
  --mount "type=bind,src=$root_dir,dst=/release,readonly"
  # The trusted regression suite intentionally executes generated fake CLI
  # shims from tempfile. Keep the ephemeral mount non-persistent, non-root,
  # nosuid and nodev, but explicitly allow that test execution.
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m,mode=1777
)

if [[ -n "${CONTROL_PLANE_VALIDATION_RESULT_DIR:-}" ]]; then
  install -d -m 0700 "$CONTROL_PLANE_VALIDATION_RESULT_DIR"
  result_dir="$(cd "$CONTROL_PLANE_VALIDATION_RESULT_DIR" && pwd -P)"
  case "$result_dir" in
    /|*','*|*$'\n'*)
      echo "BLOCKED container validation: result path is unsafe." >&2
      exit 64
      ;;
  esac
  docker_options+=(
    --mount "type=bind,src=$result_dir,dst=/results"
    --env E2E_RESULT_DIR=/results
  )
fi

while IFS='=' read -r name _value; do
  case "$name" in
    AUTOMATION_ADMIN_PASSWORD|E2E_BEARER_TOKEN|E2E_*)
      [[ "$name" == "E2E_RESULT_DIR" ]] || docker_options+=(--env "$name")
      ;;
  esac
done < <(env)

exec docker run "${docker_options[@]}" "$image" "$@"
