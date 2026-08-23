#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${AUTOMATION_TEST_PYCACHE_PREFIX:-/tmp/automation_master_test_pycache_$(id -u)}"
export PYTHONPATH="$root_dir/master_node:$root_dir/master_node/src"
bash -n "$root_dir"/*.sh "$root_dir"/scripts/*.sh
grep -F 'prepare_compose_secrets.sh' "$root_dir/deploy.sh" >/dev/null
grep -F 'automation-master-node:0.26.0' "$root_dir/compose.yaml" >/dev/null
python3 -m compileall -q "$root_dir/master_node/src"
python3 "$root_dir/tools/check_code_standards.py" master \
  "$root_dir/master_node/src" "$root_dir/master_node/test"
for test_file in "$root_dir"/master_node/test/test_*.py; do
  python3 "$test_file"
done
if command -v docker >/dev/null 2>&1; then
  docker compose -f "$root_dir/compose.yaml" config --quiet
fi
echo "Master syntax, contract and unit tests passed."
