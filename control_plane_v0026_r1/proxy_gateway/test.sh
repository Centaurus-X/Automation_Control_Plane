#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
release_root="$(cd "$root_dir/.." && pwd)"
if [[ "${CONTROL_PLANE_VALIDATION_ACTIVE:-}" != "/release" ]]; then
  exec "$release_root/tools/run_in_validation_container.sh" \
    /release/proxy_gateway/test.sh "$@"
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${AUTOMATION_TEST_PYCACHE_PREFIX:-/tmp/automation_proxy_test_pycache_$(id -u)}"
export PYTHONPATH="$root_dir/proxy_gateway:$root_dir/proxy_gateway/src"
bash -n "$root_dir"/*.sh "$root_dir"/scripts/*.sh
grep -F 'prepare_compose_secrets.sh' "$root_dir/deploy.sh" >/dev/null
grep -F 'automation-proxy-gateway:0.26.0' "$root_dir/compose.yaml" >/dev/null
python3 -m compileall -q "$root_dir/proxy_gateway/src"
python3 "$root_dir/tools/check_code_standards.py" proxy \
  "$root_dir/proxy_gateway/src" "$root_dir/proxy_gateway/test"
for test_file in "$root_dir"/proxy_gateway/test/test_*.py; do
  python3 "$test_file"
done
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$root_dir/compose.yaml" config --quiet
fi
echo "Proxy syntax, security-policy and unit tests passed."
