#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_dir="$(mktemp -d)"
call_log="$temporary_dir/docker-calls"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

source "$root_dir/scripts/control_plane_lifecycle.sh"

mock_failure=""
docker() {
  printf '%q ' "$@" >> "$call_log"
  printf '\n' >> "$call_log"
  if [[ -n "$mock_failure" && "$*" == *"$mock_failure"* ]]; then
    return 73
  fi
  return 0
}

validate_control_plane_compose_files "$root_dir"
[[ "$(grep -Fc 'compose config --quiet ' "$call_log")" == "5" ]]
prepare_control_plane_images "$root_dir" >/dev/null

grep -F 'pull eclipse-mosquitto:2.1.2-alpine ' "$call_log" >/dev/null
runtime_build="$(grep -F \
  "build --pull --no-cache --tag automation-python-free-threaded:3.14.6-v14 $root_dir/runtime/python314t " \
  "$call_log")"
[[ -n "$runtime_build" ]]
grep -F "build --pull --no-cache --tag automation-postgres:18.4-v14 $root_dir/postgres_system " \
  "$call_log" >/dev/null
grep -F 'image inspect automation-python-free-threaded:3.14.6-v14 ' \
  "$call_log" >/dev/null
master_build="$(grep -F -- "--tag automation-master-node:0.26.0 $root_dir/master_node " \
  "$call_log")"
[[ "$master_build" == *'--build-arg PYTHON_RUNTIME_IMAGE=automation-python-free-threaded:3.14.6-v14'* ]]
[[ "$master_build" == *'--no-cache'* ]]
[[ "$master_build" != *'--pull'* ]]
grep -F "build --pull --no-cache --tag automation-proxy-gateway:0.26.0 $root_dir/proxy_gateway " \
  "$call_log" >/dev/null
grep -F "build --pull --no-cache --tag automation-mcp-controller-server:0.1.0 $root_dir/mcp_controller_server " \
  "$call_log" >/dev/null
while IFS= read -r build_call; do
  [[ "$build_call" == *'--no-cache'* ]] || {
    echo "Image staging contains a cached Docker build: $build_call" >&2
    exit 1
  }
done < <(grep '^build ' "$call_log")

runtime_line="$(grep -n -F "$runtime_build" "$call_log" | head -n 1 | cut -d: -f1)"
master_line="$(grep -n -F "$master_build" "$call_log" | head -n 1 | cut -d: -f1)"
[[ -n "$runtime_line" && -n "$master_line" ]]
(( runtime_line < master_line ))
if grep -Ei 'worker|automation-lab-v2' "$call_log" >/dev/null; then
  echo "Image staging attempted to build or manage a Worker." >&2
  exit 1
fi
[[ "${CONTROL_PLANE_IMAGES_PREPARED:-0}" == "1" ]]

# A missing locally built Master base must stop staging before any dependent
# component build and before the prepared marker can be exported.
: > "$call_log"
unset CONTROL_PLANE_IMAGES_PREPARED
mock_failure='image inspect automation-python-free-threaded:3.14.6-v14'
set +e
(
  set -e
  prepare_control_plane_images "$root_dir" >/dev/null
)
failure_status="$?"
set -e
[[ "$failure_status" == "73" ]]
grep -F 'image inspect automation-python-free-threaded:3.14.6-v14 ' \
  "$call_log" >/dev/null
if grep -E 'automation-postgres|automation-master-node|automation-proxy-gateway|automation-mcp-controller-server' \
    "$call_log" >/dev/null; then
  echo "Image staging continued after the runtime image inspection failed." >&2
  exit 1
fi
[[ "${CONTROL_PLANE_IMAGES_PREPARED:-0}" == "0" ]]

echo "PASS no-cache image staging, local runtime ordering, MCP build, fail-fast base inspection, no-pull Master and external Worker boundary."
