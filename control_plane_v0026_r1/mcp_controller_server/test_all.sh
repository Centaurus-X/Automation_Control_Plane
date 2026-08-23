#!/usr/bin/env bash
set -euo pipefail

# Run dependency-free static, unit, integration and packaging preflight checks.
release_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$release_root/src:$release_root"
export TMPDIR="${TMPDIR:-$release_root/.test-tmp}"
mkdir -p "$TMPDIR"

python3 "$release_root/tools/check_code_standards.py"
python3 "$release_root/tools/check_release_structure.py"
python3 -m mcp_controller_server.main \
  --config "$release_root/config/controller_config.json" \
  check-config --skip-runtime-files
python3 "$release_root/tests/run_tests.py"

find "$release_root" -type f -name '*.sh' -exec bash -n {} \;

echo "PASS shell syntax"
echo "PASS complete release validation"
