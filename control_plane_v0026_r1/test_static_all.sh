#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${CONTROL_PLANE_VALIDATION_ACTIVE:-}" != "/release" ]]; then
  exec "$root_dir/tools/run_in_validation_container.sh" \
    /release/test_static_all.sh "$@"
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/check_release_code_standards.py"
"$root_dir/mqtt_broker/test_static.sh"
"$root_dir/postgres_system/test_static.sh"
"$root_dir/master_node/test.sh"
"$root_dir/proxy_gateway/test.sh"
CONTROL_PLANE_REFERENCE_ROOT="$root_dir" "$root_dir/mcp_controller_server/test_all.sh"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root_dir/tools" \
  python3 "$root_dir/tools/test_prepare_mcp_auth.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_mcp_bundle_integration.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root_dir/tools" \
  python3 "$root_dir/tools/test_file_inventory.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/build_master_v2_fixture.py" --check
PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/test_release_packaging.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/test_validation_container.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_rollout_bootstrap.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root_dir/tools" \
  python3 "$root_dir/tools/test_rollout_automation.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_remote_lifecycle.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root_dir/tools" \
  python3 "$root_dir/tools/test_worker_release_install.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_verify_worker_config_file_e2e.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_config_file_e2e_recovery.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_config_file_e2e_wal_projection.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_administration_release_set.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_verify_core_e2e_result.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/administration/tests/test_orchestrator.py"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_rollout_ssh_reconnect.py"
active_e2e="$(<"$root_dir/integration_test/CURRENT_E2E")"
[[ "$active_e2e" =~ ^e2e_v[0-9]{4,}$ ]]
"$root_dir/integration_test/$active_e2e/test_static.sh"
"$root_dir/tools/test_lifecycle.sh"
"$root_dir/tools/test_image_staging.sh"
"$root_dir/tools/test_python_runtime_compatibility.sh"
"$root_dir/tools/test_runtime_hotfixes.sh"
"$root_dir/tools/test_upgrade_state.sh"
"$root_dir/tools/test_rollout_modes.sh"
"$root_dir/tools/test_extended_recovery.sh"
"$root_dir/tools/validate_release.sh"
if [[ "${VERIFY_RELEASE_CHECKSUMS:-0}" == "1" ]]; then
  "$root_dir/tools/verify_checksums.sh"
fi
echo "All static and functional package tests passed."
