#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONDONTWRITEBYTECODE=1 python3 "$root_dir/tools/check_release_code_standards.py"
active_e2e_selector="$root_dir/integration_test/CURRENT_E2E"
[[ -s "$active_e2e_selector" ]] || {
  echo "Missing active E2E selector: integration_test/CURRENT_E2E" >&2
  exit 1
}
active_e2e="$(<"$active_e2e_selector")"
[[ "$active_e2e" =~ ^e2e_v[0-9]{4,}$ ]] || {
  echo "Invalid active E2E selector: $active_e2e" >&2
  exit 1
}
[[ "$active_e2e" == "e2e_v0025" ]] || {
  echo "Release v0026-r1 must select the qualified e2e_v0025 package, got: $active_e2e" >&2
  exit 1
}
active_e2e_dir="$root_dir/integration_test/$active_e2e"

for package in mqtt_broker postgres_system master_node proxy_gateway mcp_controller_server; do
  [[ -d "$root_dir/$package" ]] || { echo "Missing release package: $package" >&2; exit 1; }
  [[ -x "$root_dir/$package/deploy.sh" ]] || { echo "Missing executable deploy.sh: $package" >&2; exit 1; }
  [[ -x "$root_dir/$package/stop.sh" ]] || { echo "Missing executable stop.sh: $package" >&2; exit 1; }
done
[[ ! -d "$root_dir/worker_node" ]] || {
  echo "Worker code must not be bundled into the control-plane release." >&2
  exit 1
}
for forbidden_worker_path in \
    worker_v0082_extension \
    integration_test/lab_v2 \
    scripts/prepare_worker_v0082_lab.py \
    scripts/prepare_worker_v0082_lab.sh; do
  [[ ! -e "$root_dir/$forbidden_worker_path" ]] || {
    echo "Bundled Worker artifact is forbidden: $forbidden_worker_path" >&2
    exit 1
  }
done
for forbidden_validation_path in \
    requirements-host-validation.txt \
    tools/host_validation_environment.py \
    tools/run_in_host_validation_env.sh \
    tools/test_host_validation_environment.py; do
  [[ ! -e "$root_dir/$forbidden_validation_path" ]] || {
    echo "Legacy host-venv validation artifact remains: $forbidden_validation_path" >&2
    exit 1
  }
done
[[ "$(<"$root_dir/VERSION")" == "0.26.0" ]]
[[ "$(<"$root_dir/RELEASE_REVISION")" == "v0026-r1" ]]
[[ "$(<"$root_dir/master_node/RELEASE_REVISION")" == "v0026-r1" ]]
for package in mqtt_broker postgres_system; do
  [[ "$(<"$root_dir/$package/VERSION")" == "0.14.0" ]] || {
    echo "Package version is inconsistent: $package" >&2
    exit 1
  }
done
[[ "$(<"$root_dir/master_node/VERSION")" == "0.26.0" ]] || {
  echo "Master package version is inconsistent." >&2
  exit 1
}
[[ "$(<"$root_dir/proxy_gateway/VERSION")" == "0.26.0" ]] || {
  echo "Proxy package version is inconsistent." >&2
  exit 1
}
[[ "$(<"$root_dir/mcp_controller_server/VERSION")" == "0.1.0" ]] || {
  echo "MCP Controller Server package version is inconsistent." >&2
  exit 1
}
[[ "$(<"$root_dir/mcp_controller_server/RELEASE_REVISION")" == "v0001-r1" ]] || {
  echo "MCP Controller Server package revision is inconsistent." >&2
  exit 1
}
[[ "$(<"$active_e2e_dir/VERSION")" == "0.25.0-e2e.3" ]] || {
  echo "E2E package version is inconsistent." >&2
  exit 1
}
for required_file in \
    .dockerignore \
    RELEASE_REVISION \
    sbom.spdx.json \
    documentation/MASTER_V2_ANALYSE_UND_DIAGNOSE_DE.md \
    documentation/MASTER_V2_ARCHITEKTUR_DE.md \
    documentation/DEPLOYMENT_TEST_LAB_V2_DE.md \
    documentation/DIAGNOSE_ROLLOUT_V0012_PSYCOPG_CP314T_DE.md \
    documentation/DIAGNOSE_MASTER_BOOTSTRAP_V0013_DE.md \
    documentation/DIAGNOSE_MASTER_STAGE8_ACTIVATION_V0013_R2_DE.md \
    documentation/DIAGNOSE_E2E_AKTOR_LIFECYCLE_V0013_R3_DE.md \
    documentation/RELEASE_VALIDATION_V0013_DE.md \
    documentation/DIAGNOSE_UND_RELEASE_V0014_DE.md \
    documentation/RELEASE_VALIDATION_V0014_DE.md \
    documentation/DIAGNOSE_UND_RELEASE_V0015_DE.md \
    documentation/RELEASE_VALIDATION_V0015_DE.md \
    documentation/DIAGNOSE_UND_RELEASE_V0017_DE.md \
    documentation/RELEASE_VALIDATION_V0017_DE.md \
    documentation/RELEASE_VALIDATION_V0025_R1_DE.md \
    documentation/RELEASE_VALIDATION_V0026_R1_DE.md \
    documentation/DIAGNOSE_UND_INTEGRATION_V0026_R1_DE.md \
    documentation/MCP_CONTROLLER_INTEGRATION_V0026_R1_DE.md \
    documentation/CODE_AUDIT_V0017_R13_DE.md \
    documentation/ROLLOUT_AUTOMATION_ARCHITEKTUR_DE.md \
    RELEASE_NOTES_V0025_R1_DE.md \
    RELEASE_NOTES_V0026_R1_DE.md \
    ROLLOUT_AUTOMATION_ANLEITUNG_DE_MINI.md \
    ROLLOUT_AUTOMATION_ANLEITUNG_DE.md \
    ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0025_R1_DE.md \
    ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0026_R1_DE.md \
    rollout_automation.json.example \
    credentials.json.example \
    administration/orchestrate.py \
    administration/component_adapter.py \
    administration/orchestrate.sh \
    administration/inventory.example.json \
    administration/component_registry.example.json \
    administration/release_manifest.example.json \
    administration/README.md \
    administration/BETRIEBSDOKUMENTATION_DE.md \
    administration/tests/fake_adapter.py \
    administration/tests/test_orchestrator.py \
    mqtt_broker/scripts/handoff_live_pki.sh \
    mqtt_broker/scripts/runtime_directory_guard.sh \
    mqtt_broker/scripts/mqtt_delivery_probe.sh \
    tools/test_mqtt_runtime_directory_guard.sh \
    tools/test_mqtt_delivery_probe.sh \
    postgres_system/scripts/handoff_live_tls.sh \
    administration/inventory.test-lab.json \
    administration/component_registry.test-lab.json \
    administration/release_manifest.json \
    integration_test/CURRENT_E2E \
    requirements-validation.txt \
    documentation/reference/CODE_AND_ARCHITECTURE_SPEZIFICATION.md \
    documentation/reference/MASTER_V2_PLANUNGSDOKUMENTATION_DE_v1.1.md \
    documentation/reference/v0015/AUTOMATION_CONTROL_PLANE_V0015_IMPLEMENTIERUNGSVORSCHRIFT_DE.md \
    documentation/reference/v0015/E2E_V0015_TESTPLAN_DE.md \
    documentation/reference/v0015/QUELLEN_UND_AENDERUNGSMATRIX.md \
    documentation/reference/v0015/README_RELEASE_EINORDNUNG.md \
    documentation/reference/v0015/protocol/ACTION_PAYLOAD_CONTRACTS_DE.md \
    runtime/python314t/Dockerfile \
    runtime/python314t/sbom.spdx.json \
    master_node/master_node/deployments/migrations/002_master_v2.sql \
    master_node/master_node/src/core/shard_materializer.py \
    master_node/master_node/src/core/rollout_orchestrator.py \
    master_node/master_node/src/core/proposal_ingest.py \
    master_node/master_node/src/core/cross_fact_router.py \
    master_node/master_node/src/core/scope_activation.py \
    master_node/master_node/src/library/chunk_transport.py \
    master_node/master_node/src/library/master_entity_store_postgresql.py \
    master_node/master_node/src/library/master_heap_store.py \
    master_node/master_node/src/runtime_state_probe.py \
    master_node/test_runtime_state.sh \
    master_node/master_node/test/test_master_no_execution_v2.py \
    master_node/master_node/test/test_master_v2_contracts.py \
    master_node/master_node/test/test_rollout_orchestrator_v2.py \
    master_node/master_node/test/test_node_capabilities.py \
    master_node/master_node/test/test_inbox_json_persistence.py \
    master_node/master_node/test/test_master_bootstrap_readiness.py \
    master_node/master_node/test/test_initial_seed_pipeline.py \
    master_node/master_node/test/test_node_sync_without_cross.py \
    master_node/master_node/test/test_worker_replica_readiness.py \
    master_node/master_node/test/test_operation_lifecycle.py \
    master_node/master_node/test/test_operation_expiry.py \
    master_node/master_node/test/test_authority_fencing.py \
    master_node/master_node/test/test_ack_lease_fencing.py \
    master_node/master_node/test/test_operation_result_fencing.py \
    master_node/master_node/test/test_latest_state_ordering.py \
    master_node/master_node/test/test_legacy_sensor_sequence_compatibility.py \
    master_node/master_node/test/test_runtime_state_probe.py \
    master_node/master_node/_config/application/_legacy/initial_config.json \
    master_node/master_node/_config/application/_legacy/initial_config_v2.json \
    master_node/master_node/_config/application/initial_config_v9.json \
    master_node/master_node/_config/application/RULE_MIGRATION_REPORT.json \
    integration_test/README.md \
    integration_test/exercise_lab_v2.sh \
    integration_test/exercise_lab_v2_api.py \
    integration_test/scenarios/CONTROL_PLANE_ACCEPTANCE_DE.md \
    integration_test/e2e_v0025/control_plane_wss_e2e_test.py \
    integration_test/e2e_v0025/config_commit_path_equivalence_e2e.py \
    integration_test/e2e_v0025/worker_config_file_e2e.py \
    integration_test/e2e_v0025/fn02_protocol_simulator.py \
    integration_test/e2e_v0025/worker_cross_test_launcher.py \
    integration_test/e2e_v0025/run_e2e.sh \
    integration_test/e2e_v0025/test_static.sh \
    integration_test/e2e_v0025/README_E2E_V0025_DE.md \
    integration_test/e2e_v0025/requirements-e2e.txt \
    integration_test/e2e_v0025/VERSION \
    integration_test/e2e_v0025/SHA256SUMS \
    runtime/validation/Dockerfile \
    tools/run_in_validation_container.sh \
    tools/test_validation_container.py \
    tools/build_release_archive.py \
    tools/build_master_v2_fixture.py \
    tools/test_release_packaging.py \
    proxy_gateway/proxy_gateway/test/test_session_logout_sql.py \
    test_worker_auto_admission.sh \
    test_worker_sensor_telemetry.sh \
    test_e2e.sh \
    run_core_e2e_lab.sh \
    diagnose_worker_sync.sh \
    master_node/master_node/test/test_worker_auto_admission.py \
    scripts/import_runtime_state.sh \
    tools/test_upgrade_state.sh \
    tools/test_rollout_modes.sh \
    tools/test_image_staging.sh \
    tools/test_python_runtime_compatibility.sh \
    tools/test_extended_recovery.sh \
    proxy_gateway/proxy_gateway/test/test_asgi_interface.py \
    proxy_gateway/proxy_gateway/test/test_async_websockets_server.py \
    proxy_gateway/proxy_gateway/test/test_container_dependency_bootstrap.py \
    proxy_gateway/proxy_gateway/test/test_client_command_dispatcher.py \
    proxy_gateway/proxy_gateway/test/test_event_stream.py \
    proxy_gateway/proxy_gateway/test/test_metrics_endpoint.py \
    proxy_gateway/proxy_gateway/test/test_local_action_recovery.py \
    proxy_gateway/proxy_gateway/src/core/client_command_dispatcher.py \
    proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py \
    proxy_gateway/proxy_gateway/src/library/generate_ssl_certs.py \
    proxy_gateway/tools/check_python_dependencies.py \
    proxy_gateway/tools/container_dependency_bootstrap.py \
    proxy_gateway/scripts/generate_https_test_cert.sh \
    proxy_gateway/proxy_gateway/deployments/migrations/002_proxy_gateway_v0015.sql \
    tools/test_runtime_hotfixes.sh \
    proxy_gateway/proxy_gateway/test/test_role_grants.py \
    proxy_gateway/proxy_gateway/test/test_api_token_scopes.py \
    proxy_gateway/proxy_gateway/test/test_gateway_config.py \
    proxy_gateway/proxy_gateway/src/database_security_probe.py \
    mqtt_broker/scripts/mqtt_acl_probe.sh \
    documentation/DIAGNOSE_MQTT_STAGE4_ACL_ORACLE_R6_DE.md \
    mqtt_broker/scripts/prepare_runtime_config.sh \
    mqtt_broker/scripts/prepare_compose_secrets.sh \
    postgres_system/scripts/prepare_compose_secrets.sh \
    master_node/scripts/prepare_compose_secrets.sh \
    proxy_gateway/scripts/prepare_compose_secrets.sh \
    tools/test_worker_auto_admission_static.py \
    rollout_automation.sh \
    tools/credential_reader.py \
    tools/read_rollout_config.py \
    tools/ssh_askpass.py \
    tools/write_rollout_summary.py \
    tools/install_worker_release.py \
    tools/worker_remote_lifecycle.sh \
    tools/verify_core_e2e_result.py \
    tools/test_verify_core_e2e_result.py \
    tools/read_node_local_proposal_proof.py \
    tools/verify_worker_config_file_e2e.py \
    tools/test_rollout_bootstrap.py \
    tools/test_rollout_automation.py \
    tools/test_worker_remote_lifecycle.py \
    tools/test_worker_release_install.py \
    tools/test_verify_worker_config_file_e2e.py \
    tools/test_worker_config_file_e2e_recovery.py \
    tools/test_worker_config_file_e2e_wal_projection.py \
    tools/test_administration_release_set.py \
    tools/prepare_mcp_auth.py \
    tools/test_prepare_mcp_auth.py \
    tools/test_mcp_bundle_integration.py \
    tools/build_file_inventory.py \
    tools/test_file_inventory.py \
    documentation/automation_control_plane_v0026_r1_file_inventory.csv \
    tools/test_rollout_ssh_reconnect.py \
    mcp_controller_server/VERSION \
    mcp_controller_server/RELEASE_REVISION \
    mcp_controller_server/RELEASE_METADATA.json \
    mcp_controller_server/SHA256SUMS \
    mcp_controller_server/Dockerfile \
    mcp_controller_server/compose.yaml \
    mcp_controller_server/config/controller_config.json \
    mcp_controller_server/test_all.sh \
    mcp_controller_server/deploy.sh \
    mcp_controller_server/ready.sh \
    mcp_controller_server/status.sh \
    mcp_controller_server/backup.sh \
    mcp_controller_server/rollback.sh \
    mcp_controller_server/secrets/README.md \
    master_node/master_node/test/test_worker_runtime_recovery.py \
    master_node/master_node/test/test_sensor_sequence_floor.py \
    master_node/master_node/test/test_proposal_ingest_v2.py; do
  [[ -f "$root_dir/$required_file" ]] || {
    echo "Missing required security gate: $required_file" >&2
    exit 1
  }
done

for forbidden_file in \
    master_node/master_node/src/core/_automation_thread.py \
    master_node/master_node/src/core/automation_authority.py \
    master_node/master_node/src/core/global_automation.py \
    master_node/master_node/src/library/_automation_engine.py \
    master_node/master_node/src/library/_event_interface.py \
    master_node/master_node/src/library/_generic_finite_state_machine.py \
    master_node/master_node/src/library/_generic_rule_engine.py \
    master_node/master_node/src/library/_state_shard_manager.py; do
  [[ ! -e "$root_dir/$forbidden_file" ]] || {
    echo "Forbidden legacy Master execution module remains: $forbidden_file" >&2
    exit 1
  }
done

if [[ "${VALIDATE_ARCHIVE_TREE:-0}" == "1" ]]; then
  sensitive_file="$(find "$root_dir" -type f \
    \( -name 'credentials.json' \
       -o -name 'rollout_automation.json' \
       -o -name 'rollout_automation.env' \
       -o -name '*.key' -o -name '*.pem' -o -name '*.p12' -o -name '*.pfx' \
       -o -name '*.pyc' \) -print -quit)"
  generated_directory="$(find "$root_dir" -type d \
    \( -name __pycache__ -o -name .git \
       -o \( -name secrets ! -path "$root_dir/mcp_controller_server/secrets" \) \
       -o -name test-pki -o -name runtime-config -o -name runtime-pki-handoff \
       -o -name runtime-tls-handoff -o -name backup -o -name backups \
       -o -name results -o -name .venv -o -name venv \
       -o -path '*/integration_test/generated' \
       -o -path '*/integration_test/runtime' \
       -o -path '*/integration_test/e2e_v0025/runtime' \) -print -quit)"
else
  sensitive_file="$(find "$root_dir" \
    \( -path "$root_dir/mqtt_broker/secrets" \
       -o -path "$root_dir/mqtt_broker/test-pki" \
       -o -path "$root_dir/mqtt_broker/runtime-config" \
       -o -path "$root_dir/mqtt_broker/runtime-pki-handoff" \
       -o -path "$root_dir/mqtt_broker/backup" \
       -o -path "$root_dir/postgres_system/secrets" \
       -o -path "$root_dir/postgres_system/tls" \
       -o -path "$root_dir/postgres_system/runtime-tls-handoff" \
       -o -path "$root_dir/postgres_system/backup" \
       -o -path "$root_dir/master_node/secrets" \
       -o -path "$root_dir/proxy_gateway/secrets" \
       -o -path "$root_dir/integration_test/generated" \
       -o -path "$root_dir/integration_test/runtime" \) -prune \
    -o -type f \( -name 'credentials.json' \
       -o -name 'rollout_automation.json' \
       -o -name 'rollout_automation.env' \
       -o -name '*.key' -o -name '*.pem' -o -name '*.p12' -o -name '*.pfx' \
       -o -name '*.pyc' \) -print -quit)"
  generated_directory="$(find "$root_dir" \
    \( -path "$root_dir/mqtt_broker/secrets" \
       -o -path "$root_dir/mqtt_broker/test-pki" \
       -o -path "$root_dir/mqtt_broker/runtime-config" \
       -o -path "$root_dir/mqtt_broker/runtime-pki-handoff" \
       -o -path "$root_dir/mqtt_broker/backup" \
       -o -path "$root_dir/postgres_system/secrets" \
       -o -path "$root_dir/postgres_system/tls" \
       -o -path "$root_dir/postgres_system/runtime-tls-handoff" \
       -o -path "$root_dir/postgres_system/backup" \
       -o -path "$root_dir/master_node/secrets" \
       -o -path "$root_dir/proxy_gateway/secrets" \
       -o -path "$root_dir/integration_test/generated" \
       -o -path "$root_dir/integration_test/runtime" \) -prune \
    -o -type d -name __pycache__ -print -quit)"
fi
if [[ -n "$sensitive_file" ]]; then
  echo "Release tree contains local configuration, credentials, private-key material or Python bytecode." >&2
  exit 1
fi
mcp_secret_payload=""
if [[ -d "$root_dir/mcp_controller_server/secrets" ]]; then
  mcp_secret_payload="$(find "$root_dir/mcp_controller_server/secrets" \
    -mindepth 1 ! -path "$root_dir/mcp_controller_server/secrets/README.md" \
    -print -quit)"
fi
if [[ -n "$mcp_secret_payload" ]]; then
  echo "Release tree contains MCP runtime secret material instead of only secrets/README.md." >&2
  exit 1
fi
if [[ -n "$generated_directory" ]]; then
  echo "Release tree contains generated cache or runtime-secret directories." >&2
  exit 1
fi

while IFS= read -r script; do
  [[ -x "$script" ]] || { echo "Shell script is not executable: $script" >&2; exit 1; }
done < <(find "$root_dir" -type f -name '*.sh' -print)

PYTHONDONTWRITEBYTECODE=1 python3 - "$root_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for path in sorted(root.rglob("*.json")):
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit("Invalid JSON {}: {}".format(path, error))
PY

diff -qr \
  "$root_dir/master_node/master_node/src/library/_generic_system_contract_library" \
  "$root_dir/proxy_gateway/proxy_gateway/src/library/_generic_system_contract_library" \
  >/dev/null
cmp \
  "$root_dir/master_node/master_node/src/library/_generic_type_library.py" \
  "$root_dir/proxy_gateway/proxy_gateway/src/library/_generic_type_library.py" \
  >/dev/null
for legacy_contract_root in \
  "$root_dir/contracts_src" \
  "$root_dir/master_node/packages/automation_contracts" \
  "$root_dir/proxy_gateway/packages/automation_contracts"; do
  [[ ! -e "$legacy_contract_root" ]] || {
    echo "Legacy contract root remains: $legacy_contract_root" >&2
    exit 1
  }
done

if grep -R -E 'MQTT_TLS_ENABLED=(false|0)|MQTT_PORT=.*1883' \
    "$root_dir/deploy_test_lab.sh" "$root_dir/master_node/deploy_test.sh" \
    "$root_dir/proxy_gateway/deploy_test.sh" >/dev/null; then
  echo "Lab rollout contains an insecure MQTT setting." >&2
  exit 1
fi
grep -F 'prepare_control_plane_deployment' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'prepare_control_plane_deployment' "$root_dir/deploy_production.sh" >/dev/null
grep -F 'tools/verify_checksums.sh' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'tools/verify_checksums.sh' "$root_dir/deploy_production.sh" >/dev/null
grep -F 'docker kill' "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null
grep -F 'mosquitto --test-config' "$root_dir/mqtt_broker/setup.sh" >/dev/null
grep -F -- '-i automation-mqtt-health' "$root_dir/mqtt_broker/compose.yaml" >/dev/null
[[ -x "$root_dir/mqtt_broker/scripts/handoff_live_pki.sh" ]]
grep -F 'Active MQTT trust domain was verified' \
  "$root_dir/mqtt_broker/scripts/handoff_live_pki.sh" >/dev/null
grep -F 'test MQTT PKI cannot be promoted' \
  "$root_dir/mqtt_broker/scripts/handoff_live_pki.sh" >/dev/null
grep -F 'mqtt_allow_verified_active_secret_destination' \
  "$root_dir/mqtt_broker/scripts/handoff_live_pki.sh" >/dev/null
[[ -x "$root_dir/mqtt_broker/scripts/runtime_directory_guard.sh" ]]
[[ -x "$root_dir/tools/test_mqtt_runtime_directory_guard.sh" ]]
"$root_dir/tools/test_mqtt_runtime_directory_guard.sh"
[[ -x "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" ]]
[[ -x "$root_dir/tools/test_mqtt_delivery_probe.sh" ]]
grep -F 'verify_exact_mqtt_delivery worker master' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'local convergence_attempts=3' \
  "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" >/dev/null
grep -F 'local observation_attempts=20' \
  "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" >/dev/null
grep -F 'local subscriber_timeout_s=120' \
  "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" >/dev/null
grep -F -- '-t "$topic" -W "$subscriber_timeout_s"' \
  "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" >/dev/null
grep -F 'bounded SUBACK challenges' \
  "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh" >/dev/null
grep -F 'delayed_readiness)' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
grep -F 'delayed_publisher_startup)' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
grep -F 'no_readiness)' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
grep -F 'no_delivery)' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
grep -F 'publish_count >= 2' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
grep -F 'subscriber_timeout_s >= 90' \
  "$root_dir/tools/test_mqtt_delivery_probe.sh" >/dev/null
if grep -Eq 'readiness_attempts=|delivery_attempts=|sleep (10|11|30|60)([^.]|$)' \
    "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh"; then
  echo "MQTT delivery probe reintroduced a fixed readiness wait." >&2
  exit 1
fi
"$root_dir/tools/test_mqtt_delivery_probe.sh"
[[ -x "$root_dir/postgres_system/scripts/handoff_live_tls.sh" ]]
grep -F 'Active PostgreSQL TLS domain was verified' \
  "$root_dir/postgres_system/scripts/handoff_live_tls.sh" >/dev/null
grep -F 'test PostgreSQL TLS cannot be promoted' \
  "$root_dir/postgres_system/scripts/handoff_live_tls.sh" >/dev/null
grep -F 'POSTGRES_CA_SOURCE' \
  "$root_dir/administration/component_adapter.py" >/dev/null
[[ -x "$root_dir/postgres_system/test_running.sh" ]]
grep -F 'test_name = "test_running.sh" if arguments.component == "database"' \
  "$root_dir/administration/component_adapter.py" >/dev/null
grep -F 'scripts/verify_role_credentials.sh' \
  "$root_dir/postgres_system/test_running.sh" >/dev/null
grep -F 'scripts/verify_replication.sh' \
  "$root_dir/postgres_system/test_running.sh" >/dev/null
if grep -Eq 'deploy(_test)?\.sh|docker compose (up|restart|down)' \
    "$root_dir/postgres_system/test_running.sh"; then
  echo "Administration PostgreSQL live test reintroduced a deployment mutation." >&2
  exit 1
fi
grep -F "./postgres_system/runtime-tls-handoff" \
  "$root_dir/tools/verify_checksums.sh" >/dev/null
grep -F '.live_postgres_tls_handoff' \
  "$root_dir/tools/test_rollout_bootstrap.py" >/dev/null
grep -F 'mqtt_persistence_probe_' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
if grep -F 'nodes/master-01/status' "$root_dir/mqtt_broker/test.sh" >/dev/null; then
  echo "MQTT persistence test collides with the live Master status topic." >&2
  exit 1
fi
grep -F '"mqtt_bind_address": "192.168.0.82"' \
  "$root_dir/administration/inventory.test-lab.json" >/dev/null
grep -F -- '--user "$(id -u):$(id -g)"' "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'verify_publish_denied worker proxy master' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'verify_publish_denied proxy worker master' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'verify_subscription_denied proxy worker' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'events/security-$$/master-01' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
if grep -F 'events/security-$$/fn-02' \
    "$root_dir/mqtt_broker/test.sh" >/dev/null; then
  echo "Proxy ACL probe uses a Worker event topic as its allowed read path." >&2
  exit 1
fi
grep -F 'topic read automation/v1/site-main/nodes/+/heartbeat' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
grep -F 'topic read automation/v1/site-main/events/+/master-01' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
[[ "$(grep -Fc 'topic read automation/v1/site-main/events/sensor_value_update/master-01' \
  "$root_dir/mqtt_broker/config/acl.conf")" == "2" ]]
grep -F 'auto_admit_broker_bound_workers' \
  "$root_dir/master_node/master_node/_config/application/master_config.json" >/dev/null
grep -F 'MASTER_AUTO_ADMIT_BROKER_BOUND_WORKERS' \
  "$root_dir/master_node/compose.yaml" >/dev/null
grep -F 'register_identity_heartbeat_handler' \
  "$root_dir/master_node/master_node/src/core/coordinator_service.py" >/dev/null
grep -F 'capabilities=COALESCE' \
  "$root_dir/master_node/master_node/src/library/node_store_postgresql.py" >/dev/null
grep -F '"release_profile": "lab_v2"' \
  "$root_dir/master_node/master_node/_config/application/master_config.json" >/dev/null
grep -F 'initial_config_v9.json' \
  "$root_dir/master_node/master_node/_config/application/master_config.json" >/dev/null
grep -F 'master_automation_execution": False' \
  "$root_dir/master_node/master_node/src/core/master_runtime.py" >/dev/null
grep -F '_ensure_authority_config(context)' \
  "$root_dir/master_node/master_node/src/core/master_runtime.py" >/dev/null
grep -F 'config_ready = _authority_config_ready(context)' \
  "$root_dir/master_node/master_node/src/core/master_runtime.py" >/dev/null
grep -F '_refresh_standby_authority_state(context)' \
  "$root_dir/master_node/master_node/src/core/master_runtime.py" >/dev/null
grep -F 'Cross-independent Node scopes' \
  "$root_dir/master_node/master_node/test/test_master_bootstrap_readiness.py" >/dev/null
grep -F 'Node Full sync is independent of Cross configuration' \
  "$root_dir/master_node/master_node/test/test_node_sync_without_cross.py" >/dev/null
grep -F 'master_node/test_runtime_state.sh' \
  "$root_dir/test_running_all.sh" >/dev/null
grep -F 'jsonb(json_ready(result))' \
  "$root_dir/master_node/master_node/src/library/authority_store_postgresql.py" >/dev/null
grep -F 'context["site_id"], context["master_instance_id"]' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/mqtt_gateway_interface.py" >/dev/null
grep -F 'PROTOCOL_V1' \
  "$root_dir/master_node/master_node/src/core/rollout_orchestrator.py" >/dev/null
grep -F 'distributed.config.chunk' \
  "$root_dir/master_node/master_node/src/library/_generic_system_contract_library/protocol_contracts.py" >/dev/null
grep -F 'CREATE TABLE IF NOT EXISTS scope_heads' \
  "$root_dir/master_node/master_node/deployments/migrations/002_master_v2.sql" >/dev/null
grep -F 'expected_value_hash' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" >/dev/null
grep -F 'converge_node_proposals' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" >/dev/null
grep -F 'scope_targets=scope_targets' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" >/dev/null
grep -F 'staged READY/activate/retire' \
  "$root_dir/master_node/master_node/src/core/scope_activation.py" >/dev/null
grep -F 'Execution is fenced exclusively by Rules and concrete' \
  "$root_dir/master_node/master_node/src/core/scope_activation.py" >/dev/null
grep -F 'configuration_seed_exception' \
  "$root_dir/master_node/master_node/test/test_master_bootstrap_readiness.py" >/dev/null
grep -F 'full-Cross initial authority seed and activation plan' \
  "$root_dir/master_node/master_node/test/test_initial_seed_pipeline.py" >/dev/null
grep -F 'org.opencontainers.image.revision="v0026-r1"' \
  "$root_dir/master_node/Dockerfile" >/dev/null
grep -F 'python "$test_file" || exit 1;' \
  "$root_dir/master_node/Dockerfile" >/dev/null
grep -F 'automation-master-node:0.26.0' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'automation-proxy-gateway:0.26.0' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'automation-mcp-controller-server:0.1.0' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'v0026-r1' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'v0001-r1' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'MASTER_MQTT_MAX_PACKET_SIZE="${MASTER_MQTT_MAX_PACKET_SIZE:-4194304}"' \
  "$root_dir/deploy_test_lab.sh" >/dev/null
if grep -F 'MASTER_MQTT_MAX_PACKET_SIZE="${MASTER_MQTT_MAX_PACKET_SIZE:-8192}"' \
    "$root_dir/deploy_test_lab.sh" >/dev/null; then
  echo "Lab deployment reintroduced the invalid 8192-byte rollout limit." >&2
  exit 1
fi
grep -F 'serialized_bytes=serialized_bytes' \
  "$root_dir/master_node/master_node/src/core/rollout_orchestrator.py" >/dev/null
grep -F 'authority_services_ready' \
  "$root_dir/master_node/master_node/src/core/cluster_orchestrator.py" >/dev/null
grep -F 'recover_new_term=True' \
  "$root_dir/master_node/master_node/src/core/cluster_orchestrator.py" >/dev/null
grep -F 'def _delivery_identity(rollout):' \
  "$root_dir/master_node/master_node/src/core/rollout_orchestrator.py" >/dev/null
grep -F 'def _rollout_transition_allowed(current, target):' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" >/dev/null
grep -F 'fingerprint(candidate) == fingerprint(current_config)' \
  "$root_dir/master_node/master_node/src/core/config_authority.py" >/dev/null
grep -F 'action plan engine differs from its rule' \
  "$root_dir/master_node/master_node/src/core/config_authority.py" >/dev/null
grep -F 'MASTER_AUTHORITY_TERM_FLOOR' \
  "$root_dir/master_node/master_node/src/system/bootstrap_system_interface.py" >/dev/null
grep -F 'snapshot["fingerprint"]' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" >/dev/null
grep -F 'worker_not_synchronised' \
  "$root_dir/master_node/master_node/src/core/operation_management.py" >/dev/null
grep -F 'operation_expired' \
  "$root_dir/master_node/master_node/src/library/operation_store_postgresql.py" >/dev/null
grep -F 'expires_at_s=command_expiry' \
  "$root_dir/master_node/master_node/src/core/coordinator_service.py" >/dev/null
grep -F 'PASS Outbox, ACK correlation and terminal lifecycle (11 checks)' \
  "$root_dir/master_node/master_node/test/test_operation_lifecycle.py" >/dev/null
grep -F '_test_node_local_config_proposal_ack_is_published()' \
  "$root_dir/master_node/master_node/test/test_operation_lifecycle.py" >/dev/null
grep -F -- '--profile' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'lab_v2' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'distributed.config.patch' \
  "$root_dir/master_node/master_node/src/lab_v2_probe.py" >/dev/null
grep -F 'outage-time local-first proposal' \
  "$root_dir/master_node/master_node/src/lab_v2_probe.py" >/dev/null
grep -F 'urn:automation:site-main:worker:fn-02' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/build_master_v2_fixture.py" --check >/dev/null
grep -F 'session_id=%s::uuid' \
  "$root_dir/proxy_gateway/proxy_gateway/src/library/identity_store_postgresql.py" >/dev/null
bash -n "$root_dir/test_worker_auto_admission.sh" \
  "$root_dir/test_worker_sensor_telemetry.sh"
bash -n "$root_dir/test_e2e.sh" "$root_dir/run_core_e2e_lab.sh" \
  "$root_dir/diagnose_worker_sync.sh"
grep -F 'sequence = payload.get("sensor_store_sequence")' \
  "$root_dir/master_node/master_node/src/core/coordinator_service.py" >/dev/null
grep -F 'sequence = payload.get("sequence")' \
  "$root_dir/master_node/master_node/src/core/coordinator_service.py" >/dev/null
grep -F 'captured["writes"][0]["source_sequence"] == 41' \
  "$root_dir/master_node/master_node/test/test_legacy_sensor_sequence_compatibility.py" \
  >/dev/null
grep -F "envelope #>> '{payload,source_node_id}'='\$node_id'" \
  "$root_dir/test_worker_sensor_telemetry.sh" >/dev/null
grep -F 'matching WSS event timed out: ' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" \
  >/dev/null
for guide_fragment in \
    'sh ./rollout_automation.sh' \
    '--list-releases' \
    '--archive' \
    '--fresh' \
    '--reuse' \
    '`automation_system_v0101.zip` übertragen.' \
    'integration_test/CURRENT_E2E' \
    'summary.json'; do
  grep -F -- "$guide_fragment" \
    "$root_dir/ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0026_R1_DE.md" \
    >/dev/null
done
bash -n "$root_dir/tools/run_in_validation_container.sh"
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_auto_admission_static.py" >/dev/null
grep -F 'test-e2e|e2e)' "$root_dir/rollout.sh" >/dev/null
grep -F 'Idempotency-Key' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'test_worker_auto_admission.sh' "$root_dir/run_core_e2e_lab.sh" >/dev/null
grep -F 'E2E_PASSWORD_ENV="$password_env"' \
  "$root_dir/run_core_e2e_lab.sh" >/dev/null
grep -F 'E2E_BEARER_TOKEN="${!bearer_env}"' \
  "$root_dir/run_core_e2e_lab.sh" >/dev/null
grep -F 'E2E_BEARER_TOKEN_ENV=E2E_BEARER_TOKEN' \
  "$root_dir/run_core_e2e_lab.sh" >/dev/null
grep -F 'automation_control_plane_release_v0026_r1.zip' \
  "$root_dir/ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0026_R1_DE.md" \
  >/dev/null
grep -F 'EVENT_UPDATE_PENDING_LIMIT = 256' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'align_to_high_watermark=False' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'if interleaved >= EVENT_UPDATE_PENDING_LIMIT:' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '_async_event_update_interleave_self_test(arguments)' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '_async_event_open_alignment_self_test(arguments)' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '"operation": "set"' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'controllers.{}.tags' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'scope-safe Master config runtime matrix self-test failed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'def _config_idempotency_identity(command):' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'node-local operation retry changed its idempotency identity' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'node-local operation identity collision self-test failed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'return ("operation", str(operation_id))' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'def _wait_parallel_wss_state(context, expected_clients, deadline=None):' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'Proxy WSS state did not drain to the parallel-test baseline' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'handshake_retries' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'parallel WSS stable-baseline self-test failed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'parallel WSS pending-principal self-test failed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'stable_samples >= 5' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'def _parallel_failure_is_transient(failure):' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'while pending_indexes:' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'pending_indexes.append(index)' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'bounded_sequential_state_synchronized' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'def _parallel_event_cursor(context):' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'last_event_id=int(context["parallel_event_last_event_id"])' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'align_to_high_watermark=False' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'single_shared_preflight' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'final accepted client is confirmed below' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'The dynamically added actuator must be restored while it is still part of' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'async def _restore_config(context, control):' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'automatic node-local compensation is refused' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '_persisted_restore_revalidation(recovery)' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'parallel WSS transient failure self-test failed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'retryable_rejection_codes' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'controller has no active owner lease' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'actuator owner lease did not become ready within bounded' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'lease_readiness_retries' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'unrelated actuator rejection became retryable' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'pending_principal_connections' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py" \
  >/dev/null
grep -F 'snapshot["pending_principal_connections"] == 0' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_async_websockets_server.py" \
  >/dev/null
grep -F 'minimum_live_config_revision' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'retained_stream_command_filter_removed' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'primary E2E failure:' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'WSS slow client send timed out' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py" >/dev/null
grep -F 'close(code=1013, reason="send timeout")' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py" >/dev/null
grep -F '_writer_send_timeout_scenario()' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_async_websockets_server.py" >/dev/null
grep -F 'from src.library._generic_type_library import json_ready' \
  "$root_dir/proxy_gateway/proxy_gateway/src/core/client_command_dispatcher.py" \
  >/dev/null
grep -F 'safe = json_ready(_redact(result))' \
  "$root_dir/proxy_gateway/proxy_gateway/src/core/client_command_dispatcher.py" \
  >/dev/null
grep -F 'native-postgresql-token' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_client_command_dispatcher.py" \
  >/dev/null
grep -F 'json.dumps(token_persisted, ensure_ascii=False, allow_nan=False)' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_client_command_dispatcher.py" \
  >/dev/null
grep -F 'token_duplicate = await token_dispatch(token_request)' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_client_command_dispatcher.py" \
  >/dev/null
grep -F 'assert "token" not in duplicate_reply' \
  "$root_dir/proxy_gateway/proxy_gateway/test/test_client_command_dispatcher.py" \
  >/dev/null
for worker_hash_file in \
    integration_test/e2e_v0025/README_E2E_V0025_DE.md \
    integration_test/e2e_v0025/fixtures/worker_launcher_plan.example.json \
    integration_test/e2e_v0025/test_static.sh \
    integration_test/e2e_v0025/worker_cross_test_launcher.py; do
  grep -F 'c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467' \
    "$root_dir/$worker_hash_file" >/dev/null
done
for recovery_fragment in \
    '_rollout_rearm_plan' \
    'worker_runtime_replica_recovery' \
    'attempts=0,transfer_id=NULL' \
    'delivery_generation'; do
  grep -F "$recovery_fragment" \
    "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" \
    >/dev/null
done
grep -F '"runtime_nonce": payload.get("runtime_nonce")' \
  "$root_dir/master_node/master_node/src/core/node_registry.py" >/dev/null
grep -F 'legacy_terminal["detail"] = {}' \
  "$root_dir/master_node/master_node/test/test_worker_runtime_recovery.py" >/dev/null
grep -F -- "-o -path './mqtt_broker/runtime-pki-handoff'" \
  "$root_dir/tools/verify_checksums.sh" >/dev/null
grep -F -- "-o -path './postgres_system/backup'" \
  "$root_dir/tools/verify_checksums.sh" >/dev/null
grep -F 'automation_platform_test.dump' \
  "$root_dir/tools/test_rollout_bootstrap.py" >/dev/null
grep -F 'assert_runtime_handoff_manifest_boundary' \
  "$root_dir/tools/test_rollout_bootstrap.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_rollout_bootstrap.py" >/dev/null
grep -F "stat -c '%u'" "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'must be owned by UID' "$root_dir/rollout_automation.sh" >/dev/null
if grep -F "stat -c '%U'" "$root_dir/rollout_automation.sh" >/dev/null; then
  echo "Rollout bootstrap reintroduced name-based ownership checks." >&2
  exit 1
fi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root_dir/tools" \
  python3 "$root_dir/tools/test_rollout_automation.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_remote_lifecycle.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_rollout_ssh_reconnect.py" >/dev/null
bash -n "$root_dir/rollout_automation.sh"
grep -F '# AUTONOMY CONTRACT — AUTOMATION CONTROL PLANE ROLLOUT' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'bootstrap_discover_release' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'bootstrap_install_release' "$root_dir/rollout_automation.sh" >/dev/null
grep -F -- '--list-releases' "$root_dir/rollout_automation.sh" >/dev/null
grep -F -- '--archive FILE' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'v0026-r1 wins over' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'No permanent Control-Plane release, replica-journal or log backup' \
  "$root_dir/rollout_automation.sh" >/dev/null
bash -n "$root_dir/tools/worker_remote_lifecycle.sh"
grep -F 'verify|verify-active|stop-compatible|stop|restart|install-start|status|diagnose' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
grep -F 'Installed Worker MQTT identity: PASS' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
grep -F 'Active Worker continuity: PASS' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
grep -F 'REUSE_PRESERVE_WORKER=1' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'verify active Worker identity without restart' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F -- '--reuse preserves the active Worker' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'ControlPersist=yes' "$root_dir/rollout_automation.sh" >/dev/null
if grep -F 'ControlPersist=180' "$root_dir/rollout_automation.sh" >/dev/null; then
  echo "Finite SSH ControlPersist regression was reintroduced." >&2
  exit 1
fi
grep -F 'ensure_ssh_master' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'NumberOfPasswordPrompts=0' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'WORKER_MANIFEST_SHA256' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'Worker package baseline' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'is_mutable_deployment_file' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
grep -F 'installed Worker immutable package integrity failed' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
grep -F 'integration_test/CURRENT_E2E' "$root_dir/rollout_automation.sh" >/dev/null
grep -F -- '--result "$e2e_result"' "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'e2e_result_dir="${E2E_RESULT_DIR:-$e2e_dir/results}"' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'e2e_result="$e2e_result_dir/e2e-${e2e_serial}-result.json"' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'cp -a "$e2e_result_dir/." "$log_dir/e2e-results/"' \
  "$root_dir/rollout_automation.sh" >/dev/null
if grep -F 'cp -a "$e2e_dir/results/."' \
    "$root_dir/rollout_automation.sh" >/dev/null; then
  echo "Rollout diagnostics ignored E2E_RESULT_DIR." >&2
  exit 1
fi
grep -F 'recoverably install Worker v0101 release' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'tools/install_worker_release.py' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'not validator.is_file()' \
  "$root_dir/tools/install_worker_release.py" >/dev/null
grep -F 'Worker release without Type Authority was installed' \
  "$root_dir/tools/test_worker_release_install.py" >/dev/null
if grep -F '_config_compatibility.py' \
    "$root_dir/tools/install_worker_release.py" \
    "$root_dir/tools/test_worker_release_install.py" >/dev/null; then
  echo "Worker installer reintroduced the forbidden compatibility authority." >&2
  exit 1
fi
grep -F 'stop-compatible' \
  "$root_dir/tools/worker_remote_lifecycle.sh" >/dev/null
for rollout_gate in \
    'Worker fieldbus prerequisite' \
    'Worker restart and telemetry sequence recovery' \
    'Worker JSON config-file E2E'; do
  grep -F "$rollout_gate" "$root_dir/rollout_automation.sh" >/dev/null
done
grep -F 'worker_node_local' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '_authority_journal_evidence(' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '_mirror_evidence(' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '"sensor_present": True' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '"actuator_present": True' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '"pid_target_temp_c": matrix["target_after"]' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F '"CFG-MATRIX", "PASSED"' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '"node_local_proposal_proof": mutation["node_local_proposal_proof"]' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F -- '--node-local-proposal-proof-output "$node_local_proposal_proof_file"' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'tools/read_node_local_proposal_proof.py' \
  "$root_dir/rollout_automation.sh" >/dev/null
grep -F 'current_run_correlated' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F 'wal_authority_correlated' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
grep -F 'differential_items_sha256' \
  "$root_dir/tools/verify_worker_config_file_e2e.py" >/dev/null
grep -F '_runtime_matrix_proof(matrix, checks)' \
  "$root_dir/tools/verify_core_e2e_result.py" >/dev/null
grep -F '"owned_changes": copy.deepcopy(owned_changes)' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F 'WRITE: aid=3 addr=524 .*ok=True' \
  "$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_verify_worker_config_file_e2e.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_config_file_e2e_recovery.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_worker_config_file_e2e_wal_projection.py" >/dev/null
grep -F 'database live test must remain non-deploying' \
  "$root_dir/administration/component_adapter.py" >/dev/null
grep -F 'test_operational_component_adapter_database_test_is_non_deploying' \
  "$root_dir/administration/tests/test_orchestrator.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_administration_release_set.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_verify_core_e2e_result.py" >/dev/null
if grep -Eq '(^|[[:space:]])scp([[:space:]]|$)|(^|[[:space:]])unzip([[:space:]]|$)' \
    "$root_dir/rollout_automation.sh"; then
  echo "Rollout reintroduced an unverified scp/unzip Worker installation path." >&2
  exit 1
fi
grep -F 'state IN ('"'"'staged'"'"','"'"'retry_wait'"'"')' \
  "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" \
  >/dev/null
if grep -F 'state IN ('"'"'staged'"'"','"'"'sending'"'"','"'"'retry_wait'"'"')' \
    "$root_dir/master_node/master_node/src/library/master_entity_store_postgresql.py" \
    >/dev/null; then
  echo "Terminal Rollout delivery reintroduced sending as a due state." >&2
  exit 1
fi
grep -F 'deploy_test_lab.sh" --fresh --profile lab_v2' \
  "$root_dir/test_all.sh" >/dev/null
grep -F 'integration_test/CURRENT_E2E' \
  "$root_dir/test_static_all.sh" >/dev/null
for validation_entrypoint in \
    test_static_all.sh \
    proxy_gateway/test.sh \
    integration_test/e2e_v0025/test_static.sh \
    integration_test/e2e_v0025/run_e2e.sh; do
  grep -F 'tools/run_in_validation_container.sh' \
    "$root_dir/$validation_entrypoint" >/dev/null
done
grep -F 'COPY requirements-validation.txt' \
  "$root_dir/runtime/validation/Dockerfile" >/dev/null
grep -F '!runtime/validation/Dockerfile' "$root_dir/.dockerignore" >/dev/null
grep -F 'python -m pip check' \
  "$root_dir/runtime/validation/Dockerfile" >/dev/null
grep -Fx '        procps \' \
  "$root_dir/runtime/validation/Dockerfile" >/dev/null
grep -F -- '--network host' \
  "$root_dir/tools/run_in_validation_container.sh" >/dev/null
grep -F -- '--read-only' \
  "$root_dir/tools/run_in_validation_container.sh" >/dev/null
grep -F 'integration_test/CURRENT_E2E' \
  "$root_dir/test_e2e.sh" >/dev/null
grep -F 'integration_test/CURRENT_E2E' \
  "$root_dir/run_core_e2e_lab.sh" >/dev/null
grep -F 'finally:' \
  "$root_dir/integration_test/e2e_v0025/control_plane_wss_e2e_test.py" >/dev/null
grep -F '192.168.0.3' \
  "$root_dir/integration_test/e2e_v0025"/*.py \
  "$root_dir/integration_test/e2e_v0025"/fixtures/*.json >/dev/null
grep -F 'is missing; run' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'context["dispatcher"]["dispatch"]' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/https_interface.py" >/dev/null
grep -F 'ipaddress.IPv4Address' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/https_interface.py" >/dev/null
grep -F 'entry["source_ip"] = str(entry["source_ip"])' \
  "$root_dir/proxy_gateway/proxy_gateway/src/library/audit_store.py" >/dev/null
grep -F 'authorize_role_grant' \
  "$root_dir/proxy_gateway/proxy_gateway/src/core/client_command_dispatcher.py" >/dev/null
grep -F 'effective_access' \
  "$root_dir/proxy_gateway/proxy_gateway/src/core/auth_management.py" >/dev/null
grep -F 'PROXY_ORIGIN_ALLOWLIST' "$root_dir/proxy_gateway/compose.yaml" >/dev/null
grep -F 'validate_proxy_deployment_config' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'validate_proxy_deployment_config' "$root_dir/deploy_production.sh" >/dev/null
grep -F 'validate_control_plane_compose_files' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'validate_control_plane_compose_files' "$root_dir/deploy_production.sh" >/dev/null
grep -F 'database_security_probe' "$root_dir/proxy_gateway/test_database.sh" >/dev/null
grep -F 'interface="asgi3"' \
  "$root_dir/proxy_gateway/proxy_gateway/src/gateway_main.py" >/dev/null
grep -F 'ws="none"' \
  "$root_dir/proxy_gateway/proxy_gateway/src/gateway_main.py" >/dev/null
grep -F 'runtime["wss"]["serve"]' \
  "$root_dir/proxy_gateway/proxy_gateway/src/gateway_main.py" >/dev/null
grep -F 'import websockets' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py" >/dev/null
grep -F 'websockets.serve(' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py" >/dev/null
grep -F 'automation_proxy_wss_command_latency_seconds_sum' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/https_interface.py" >/dev/null
grep -F 'automation_proxy_wss_replay_lag_events_max' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/https_interface.py" >/dev/null
grep -F 'automation_proxy_wss_event_fanout_latency_seconds_sum' \
  "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/https_interface.py" >/dev/null
grep -F 'proxy_headers=False' \
  "$root_dir/proxy_gateway/proxy_gateway/src/gateway_main.py" >/dev/null
if grep -R -E 'WebSocketRoute|from starlette.websockets|websockets-sansio' \
    "$root_dir/proxy_gateway/proxy_gateway/src" >/dev/null; then
  echo "Proxy runtime contains a forbidden Uvicorn/Starlette WSS integration." >&2
  exit 1
fi
[[ ! -e "$root_dir/proxy_gateway/proxy_gateway/src/interfaces/websocket_interface.py" ]] || {
  echo "Legacy Starlette WebSocket interface must not be shipped." >&2
  exit 1
}
grep -F '"port": 8443' \
  "$root_dir/proxy_gateway/proxy_gateway/_config/application/gateway_config.json" >/dev/null
grep -F '"port": 8444' \
  "$root_dir/proxy_gateway/proxy_gateway/_config/application/gateway_config.json" >/dev/null
grep -F 'PROXY_WSS_PORT: "8443"' "$root_dir/proxy_gateway/compose.yaml" >/dev/null
grep -F 'PROXY_HTTPS_PORT: "8444"' "$root_dir/proxy_gateway/compose.yaml" >/dev/null
grep -Fx 'websockets==16.0' "$root_dir/proxy_gateway/requirements.txt" >/dev/null
grep -Fx 'cryptography==46.0.0' "$root_dir/proxy_gateway/requirements.txt" >/dev/null
for proxy_pin in \
    'cffi==2.1.0' \
    'idna==3.18' \
    'pycparser==3.0' \
    'psycopg-binary==3.3.4' \
    'typing-extensions==4.16.0'; do
  grep -Fx "$proxy_pin" "$root_dir/proxy_gateway/requirements.txt" >/dev/null
done
for proxy_sbom_package in cffi idna pycparser psycopg-binary typing-extensions; do
  grep -F '"name": "'"$proxy_sbom_package"'"' \
    "$root_dir/proxy_gateway/sbom.spdx.json" >/dev/null
done
grep -F 'pip download' "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F -- '--no-index' "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F 'container_dependency_bootstrap.py' \
  "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F 'python "$test_file" || exit 1;' \
  "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F 'proxy_runtime:/app/proxy_gateway/_runtime' \
  "$root_dir/proxy_gateway/compose.yaml" >/dev/null
[[ "$(grep -Fc 'proxy_runtime:/app/proxy_gateway/_runtime' \
  "$root_dir/proxy_gateway/compose.yaml")" == "2" ]]
grep -F 'verify_manifest(WHEELHOUSE)' \
  "$root_dir/proxy_gateway/tools/container_dependency_bootstrap.py" >/dev/null
grep -F 'Base dependency check failed; repairing from the verified local wheelhouse.' \
  "$root_dir/proxy_gateway/tools/container_dependency_bootstrap.py" >/dev/null
grep -F 'test-pki/wss' \
  "$root_dir/proxy_gateway/scripts/generate_https_test_cert.sh" >/dev/null
grep -F 'test-pki/wss/ca.crt' \
  "$root_dir/integration_test/e2e_v0025/run_e2e.sh" >/dev/null
grep -F -- '--fresh' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F -- '--upgrade' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F -- '--fresh|--upgrade)' "$root_dir/rollout.sh" >/dev/null
grep -F 'purge_control_plane_for_fresh_install' \
  "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null
grep -F 'stale image' "$root_dir/test_running_all.sh" >/dev/null
grep -F 'org.opencontainers.image.version="0.14.0"' \
  "$root_dir/postgres_system/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.version="0.26.0"' \
  "$root_dir/master_node/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.version="0.26.0"' \
  "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.revision="v0026-r1"' \
  "$root_dir/proxy_gateway/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.revision="v0026-r1"' \
  "$root_dir/runtime/validation/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.version="0.1.0"' \
  "$root_dir/mcp_controller_server/Dockerfile" >/dev/null
grep -F 'org.opencontainers.image.revision="v0001-r1"' \
  "$root_dir/mcp_controller_server/Dockerfile" >/dev/null
grep -F -- '--tmpfs /tmp:rw,exec,nosuid,nodev,size=512m,mode=1777' \
  "$root_dir/tools/run_in_validation_container.sh" >/dev/null
grep -F '**_authority_write_fence(context, authority)' \
  "$root_dir/master_node/master_node/src/core/coordinator_service.py" >/dev/null
grep -F 'and result.get("retryable")' \
  "$root_dir/master_node/master_node/src/interfaces/legacy_worker_mqtt_interface.py" >/dev/null
grep -F 'EXCLUDED.source_sequence>sensor_latest.source_sequence' \
  "$root_dir/master_node/master_node/src/library/node_store_postgresql.py" >/dev/null
grep -F "value='\$sentinel'" \
  "$root_dir/postgres_system/scripts/test_persistence.sh" >/dev/null
grep -F 'POSTGRES_BACKUP_HOST_UID' \
  "$root_dir/postgres_system/scripts/container-entrypoint.sh" >/dev/null
grep -F 'find /backup' \
  "$root_dir/postgres_system/scripts/restore_test.sh" >/dev/null
grep -F 'listener_allow_anonymous false' \
  "$root_dir/mqtt_broker/config/mosquitto.conf" >/dev/null
grep -F 'user urn:automation:site-main:proxy:mcp-controller-01' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
grep -F 'topic read automation/v1/site-main/proxy/mcp-controller-01/acks/#' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
grep -F 'topic write automation/v1/site-main/proxy/mcp-controller-01/acks/#' \
  "$root_dir/mqtt_broker/config/acl.conf" >/dev/null
grep -F '"mcp-controller-01"' \
  "$root_dir/master_node/master_node/_config/application/master_config.json" >/dev/null
grep -F '"mcp_controller_server", "worker"' \
  "$root_dir/administration/orchestrate.py" >/dev/null
PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/tools/test_mcp_bundle_integration.py" >/dev/null
grep -F 'CONTROL_PLANE_CONFIRM_PURGE' "$root_dir/deploy_production.sh" >/dev/null
grep -F -- '--disable-gil' "$root_dir/runtime/python314t/Dockerfile" >/dev/null
grep -F 'Py_GIL_DISABLED' "$root_dir/runtime/python314t/Dockerfile" >/dev/null
grep -F 'sys._is_gil_enabled() is False' "$root_dir/master_node/Dockerfile" >/dev/null
grep -Fx 'psycopg==3.3.4' "$root_dir/master_node/requirements.txt" >/dev/null
if grep -F 'psycopg[binary]' "$root_dir/master_node/requirements.txt" >/dev/null; then
  echo "Free-threaded Master must not require the unavailable binary wheel." >&2
  exit 1
fi
grep -Fx 'psycopg[binary]==3.3.4' \
  "$root_dir/proxy_gateway/requirements.txt" >/dev/null
grep -F 'libpq-dev' "$root_dir/runtime/python314t/Dockerfile" >/dev/null
for sbom in \
  "$root_dir/sbom.spdx.json" \
  "$root_dir/master_node/sbom.spdx.json" \
  "$root_dir/runtime/python314t/sbom.spdx.json"; do
  grep -F '"name": "libpq"' "$sbom" >/dev/null
done
grep -F 'PSYCOPG_IMPL=python' "$root_dir/master_node/Dockerfile" >/dev/null
grep -F 'PSYCOPG_IMPL=binary' "$root_dir/proxy_gateway/Dockerfile" >/dev/null
cmp "$root_dir/master_node/tools/check_runtime_dependencies.py" \
  "$root_dir/proxy_gateway/tools/check_runtime_dependencies.py" >/dev/null
[[ "$(grep -c '^    build:$' "$root_dir/master_node/compose.yaml")" == "2" ]]
if grep -Eq '^  build:$' "$root_dir/master_node/compose.yaml"; then
  echo "Master Compose build keys are not nested below their services." >&2
  exit 1
fi
grep -F 'VALIDATE_ARCHIVE_TREE' \
  "$root_dir/tools/build_release_archive.py" >/dev/null
grep -F '".git",' "$root_dir/tools/build_release_archive.py" >/dev/null
grep -F '"backup",' "$root_dir/tools/build_release_archive.py" >/dev/null
grep -F 'contract_environment["PYTHONDONTWRITEBYTECODE"] = "1"' \
  "$root_dir/tools/build_release_archive.py" >/dev/null
grep -F 'environment.pop("CONTROL_PLANE_VALIDATION_ACTIVE", None)' \
  "$root_dir/tools/build_release_archive.py" >/dev/null
grep -F 'Root SHA256SUMS does not cover the release tree exactly' \
  "$root_dir/tools/verify_checksums.sh" >/dev/null
if grep -F -- 'docker build --pull --build-arg "PYTHON_RUNTIME_IMAGE=' \
    "$root_dir/scripts/control_plane_lifecycle.sh" \
    "$root_dir/master_node/test_container.sh" >/dev/null; then
  echo "Master build must not pull its release-local Python runtime image." >&2
  exit 1
fi
grep -F 'purge_control_plane_images' \
  "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null
if grep -F 'automation-lab-v2' \
    "$root_dir/scripts/control_plane_lifecycle.sh" \
    "$root_dir/status_all.sh" "$root_dir/logs_all.sh" >/dev/null; then
  echo "Control Plane lifecycle must not own Worker projects." >&2
  exit 1
fi
bundled_worker_distribution="$(find "$root_dir" -type f \
  \( -iname '*worker*.zip' -o -iname '*worker*.tar.gz' \
     -o -name 'Worker.Dockerfile' -o -name 'worker_entrypoint.sh' \) \
  -print -quit)"
if [[ -n "$bundled_worker_distribution" ]]; then
  echo "Control Plane archive contains a bundled Worker distribution." >&2
  echo "$bundled_worker_distribution" >&2
  exit 1
fi
echo "Release structure, JSON, contracts, secret hygiene and rollout invariants are valid."
