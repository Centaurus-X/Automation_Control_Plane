#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
release_root="$(cd "$root_dir/../.." && pwd)"
if [[ "${CONTROL_PLANE_VALIDATION_ACTIVE:-}" != "/release" ]]; then
  exec "$release_root/tools/run_in_validation_container.sh" \
    /release/integration_test/e2e_v0025/test_static.sh "$@"
fi
cd "$root_dir"

required_files=(
  .gitignore
  README.md
  README_E2E_V0025_DE.md
  VERSION
  requirements-e2e.txt
  run_e2e.sh
  test_static.sh
  control_plane_wss_e2e_test.py
  config_commit_path_equivalence_e2e.py
  fn02_protocol_simulator.py
  isolated_component_gates.py
  worker_config_file_e2e.py
  worker_cross_test_launcher.py
  fixtures/action_matrix_v0025.json
  fixtures/cross_worker_site-main.json
  fixtures/fn02_runtime_config.example.json
  fixtures/worker_launcher_plan.example.json
  fixtures/worker_launcher_command.example.json
  fixtures/broker_introspection_proof.example.json
  fixtures/broker_release_proof.example.json
  schemas/recovery_state.schema.json
  schemas/action_payloads.schema.json
  schemas/command_request.schema.json
  schemas/command_response.schema.json
  schemas/event_envelope.schema.json
  schemas/event_filter_update.schema.json
  schemas/event_response.schema.json
  schemas/event_subscription.schema.json
)
for path in "${required_files[@]}"; do
  [[ -f "$path" ]] || {
    echo "Missing E2E v0025 file: $path" >&2
    exit 1
  }
done

version="$(<VERSION)"
for script in \
  control_plane_wss_e2e_test.py \
  fn02_protocol_simulator.py \
  isolated_component_gates.py \
  worker_cross_test_launcher.py; do
  reported="$(PYTHONDONTWRITEBYTECODE=1 python3 "$script" --version)"
  [[ "$reported" == "$version" ]] || {
    echo "Version mismatch: $script reports $reported, expected $version" >&2
    exit 1
  }
done

PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import ast
import json
from pathlib import Path
from xml.etree import ElementTree

root = Path(".")
for path in sorted(root.rglob("*.json")):
    json.loads(path.read_text(encoding="utf-8"))

schema_paths = sorted((root / "schemas").glob("*.schema.json"))
schema_documents = {
    path.resolve(): json.loads(path.read_text(encoding="utf-8"))
    for path in schema_paths
}


def schema_refs(value):
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str):
            yield value["$ref"]
        for item in value.values():
            yield from schema_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from schema_refs(item)


def resolve_pointer(document, fragment):
    current = document
    if fragment in ("", "/"):
        return current
    if not fragment.startswith("/"):
        raise ValueError("unsupported JSON pointer fragment")
    for token in fragment[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
            continue
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
            continue
        raise ValueError("unresolved JSON pointer token " + token)
    return current


for path, document in schema_documents.items():
    if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SystemExit("schema is not Draft 2020-12: " + str(path))
    if not isinstance(document.get("$id"), str) or not document["$id"]:
        raise SystemExit("schema has no stable $id: " + str(path))
    for reference in schema_refs(document):
        target_name, separator, fragment = reference.partition("#")
        target_path = (path.parent / target_name).resolve() if target_name else path
        target = schema_documents.get(target_path)
        if target is None:
            raise SystemExit("unresolved schema file reference: {} in {}".format(
                reference, path
            ))
        try:
            resolve_pointer(target, fragment if separator else "")
        except ValueError as exc:
            raise SystemExit("unresolved schema pointer: {} in {}: {}".format(
                reference, path, exc
            ))

reference_architecture = root.resolve().parents[1] / "documentation" / "reference" \
    / "v0015" / "architecture"
for path in sorted(reference_architecture.glob("*.svg")):
    parsed = ElementTree.parse(path)
    if not parsed.getroot().tag.endswith("svg"):
        raise SystemExit("architecture SVG root differs: " + str(path))
for path in sorted(reference_architecture.glob("*.mmd")):
    content = path.read_text(encoding="utf-8").strip()
    first = content.splitlines()[0].strip() if content else ""
    if not first.startswith(("flowchart ", "sequenceDiagram", "stateDiagram")):
        raise SystemExit("unsupported or empty Mermaid source: " + str(path))

issues = []
for path in sorted(root.glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            issues.append("{}:{} class definition".format(path, node.lineno))
        if isinstance(node, ast.Lambda):
            issues.append("{}:{} lambda".format(path, node.lineno))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
            if node.decorator_list:
                issues.append("{}:{} decorator".format(path, node.lineno))
            if node.returns is not None or any(item.annotation is not None for item in arguments):
                issues.append("{}:{} type annotation".format(path, node.lineno))
            if len(arguments) > 3:
                issues.append("{}:{} more than three explicit parameters".format(path, node.lineno))
        if isinstance(node, ast.AnnAssign):
            issues.append("{}:{} annotated assignment".format(path, node.lineno))
        if isinstance(node, ast.ImportFrom) and node.module in ("typing", "dataclasses", "__future__"):
            issues.append("{}:{} forbidden import".format(path, node.lineno))
        if isinstance(node, ast.Import):
            names = {item.name for item in node.names}
            if names.intersection({"typing", "dataclasses"}):
                issues.append("{}:{} forbidden import".format(path, node.lineno))
    if path.name == "control_plane_wss_e2e_test.py":
        declared = set()
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "add_argument" and node.args:
                option = node.args[0]
                if isinstance(option, ast.Constant) and isinstance(option.value, str) \
                        and option.value.startswith("--"):
                    destination = option.value[2:].replace("-", "_")
                    for keyword in node.keywords:
                        if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant):
                            destination = str(keyword.value.value)
                    declared.add(destination)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Subscript):
                base = node.value
                if isinstance(base.value, ast.Name) and base.value.id == "context" \
                        and isinstance(base.slice, ast.Constant) \
                        and base.slice.value == "arguments":
                    used.add(node.attr)
        missing_arguments = sorted(used - declared)
        if missing_arguments:
            issues.append("{}: argparse destinations missing for {}".format(
                path, ", ".join(missing_arguments)
            ))
if issues:
    raise SystemExit("\n".join(issues))

actions = json.loads(Path("fixtures/action_matrix_v0025.json").read_text(encoding="utf-8"))
names = [item["action"] for item in actions["actions"]]
if len(names) != 23 or len(names) != len(set(names)):
    raise SystemExit("action fixture must contain exactly 23 unique actions")
recovery = json.loads(Path("schemas/recovery_state.schema.json").read_text(encoding="utf-8"))
if recovery.get("type") != "object" or recovery.get("additionalProperties") is not False:
    raise SystemExit("recovery schema must be a closed object")
runtime_fixture_path = Path("fixtures/fn02_runtime_config.example.json").resolve()
runtime_fixture = json.loads(runtime_fixture_path.read_text(encoding="utf-8"))
release_root = root.resolve().parents[1]
expected_pki_root = release_root / "mqtt_broker" / "test-pki"
for name in ("ca_file", "cert_file", "key_file"):
    resolved = (runtime_fixture_path.parent / runtime_fixture[name]).resolve()
    if expected_pki_root not in resolved.parents:
        raise SystemExit("fn-02 runtime fixture resolves outside mqtt_broker/test-pki: " + name)
print("PASS Python AST and JSON validation")
PY

grep -F 'import websockets' control_plane_wss_e2e_test.py >/dev/null
grep -F 'import jsonschema' control_plane_wss_e2e_test.py >/dev/null
grep -F '_validate_schema(context, "command_request"' control_plane_wss_e2e_test.py >/dev/null
grep -F '_validate_schema(context, "event_envelope"' control_plane_wss_e2e_test.py >/dev/null
grep -F 'websockets.connect' control_plane_wss_e2e_test.py >/dev/null
grep -F '"path": "/api/v1/audit"' control_plane_wss_e2e_test.py >/dev/null
grep -F 'headers["Idempotency-Key"] = idempotency_key' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'context["run_id"] + ":http:missing-node-drain"' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'int(retained_clients) * 4 + 1' control_plane_wss_e2e_test.py >/dev/null
grep -F 'CONFIG_CAS_ATTEMPTS = 8' control_plane_wss_e2e_test.py >/dev/null
grep -F '"CFG-MATRIX", "PASSED"' control_plane_wss_e2e_test.py >/dev/null
if grep -F '.rows"' control_plane_wss_e2e_test.py >/dev/null; then
  echo "Active V9 E2E contains a grouped I/O change path." >&2
  exit 1
fi
grep -F '"operation": "append", "path": "sensors"' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '"operation": "append", "path": "actuators"' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '"processes.{}.process_location"' control_plane_wss_e2e_test.py >/dev/null
grep -F 'proposal_ack = await _wait_proposal_result' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'authority config is not canonical V9 storage' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '"process_states.{}.mode_id"' control_plane_wss_e2e_test.py >/dev/null
grep -F '"process_states.{}.value_id"' control_plane_wss_e2e_test.py >/dev/null
grep -F '"process_states.{}.parameters.target_temp_c"' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'async def _restore_config(context, control):' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'a concurrent owner changed an E2E-owned config field' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '"retryable_rejection_codes": ("stale_revision",)' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '_async_config_restore_race_self_test(arguments)' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'foreign owner race closed the recovery journal' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'node-local stale compensation was not correlated and field-bounded' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F 'any("base_revision" in item or "changes" in item' \
  control_plane_wss_e2e_test.py >/dev/null
grep -F '"commit_paths": ["worker_node_local"]' \
  worker_config_file_e2e.py >/dev/null
grep -F 'local_json_file_sem_node_authority' worker_config_file_e2e.py >/dev/null
grep -F '"policy_fallback": ("scope_operations", "operations")' \
  worker_config_file_e2e.py >/dev/null
grep -F '"runtime": ("action_plan", "actions")' \
  worker_config_file_e2e.py >/dev/null
grep -F 'wal_authority_correlated' worker_config_file_e2e.py >/dev/null
grep -F '_authority_journal_evidence(' worker_config_file_e2e.py >/dev/null
grep -F '_mirror_evidence(' worker_config_file_e2e.py >/dev/null
grep -F 'differential_items_sha256' worker_config_file_e2e.py >/dev/null
grep -F '"sensor_present": True' worker_config_file_e2e.py >/dev/null
grep -F '"actuator_present": True' worker_config_file_e2e.py >/dev/null
grep -F '"pid_target_temp_c": matrix["target_after"]' \
  worker_config_file_e2e.py >/dev/null
grep -F 'device_state_data_v9_node_fn-01.json' worker_config_file_e2e.py >/dev/null
grep -F '_bind_worker_storage_verifier(paths["worker"])' \
  worker_config_file_e2e.py >/dev/null
grep -F '"process_data": {' worker_config_file_e2e.py >/dev/null
grep -F '"primary": True' worker_config_file_e2e.py >/dev/null
grep -F -- '--https-url' run_e2e.sh >/dev/null
grep -F -- '--wss-url' run_e2e.sh >/dev/null
grep -F 'https://192.168.0.82:8444' run_e2e.sh >/dev/null
grep -F 'wss://192.168.0.82:8443' run_e2e.sh >/dev/null
grep -F 'automation.mqtt.v1' fn02_protocol_simulator.py >/dev/null
grep -F 'PROTOCOL_V2' fn02_protocol_simulator.py >/dev/null
grep -F 'c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467' \
  worker_cross_test_launcher.py >/dev/null
grep -Fx 'websockets==16.0' requirements-e2e.txt >/dev/null

for schema in schemas/*.schema.json; do
  case "$(basename "$schema")" in
    recovery_state.schema.json|action_payloads.schema.json) continue ;;
  esac
  reference="$release_root/documentation/reference/v0015/protocol/$(basename "$schema")"
  [[ -f "$reference" ]] || continue
  cmp -s "$schema" "$reference" || {
    echo "E2E schema differs from normative v0015 protocol copy: $schema" >&2
    exit 1
  }
done

if grep -R -n -E --include='*.py' --include='*.sh' --exclude='test_static.sh' \
    -- '(--insecure|CERT_NONE|_create_unverified_context)' .; then
  echo "Insecure TLS bypass found in E2E v0025." >&2
  exit 1
fi
if grep -R -n -E --include='*.py' \
    -- '(^class |@dataclass|from typing|import typing|lambda )' .; then
  echo "Forbidden object/type construct found in E2E v0025." >&2
  exit 1
fi
if find . -type f \( -name '*.key' -o -name '*.pem' -o -name '*.p12' \) -print -quit \
    | grep -q .; then
  echo "Private key material is forbidden in the E2E package." >&2
  exit 1
fi
if find . -type d -name 'automation_system_v0101' -print -quit | grep -q .; then
  echo "Worker source must remain external to the Control Plane E2E." >&2
  exit 1
fi

bash -n run_e2e.sh test_static.sh
PYTHONDONTWRITEBYTECODE=1 python3 control_plane_wss_e2e_test.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 config_commit_path_equivalence_e2e.py
PYTHONDONTWRITEBYTECODE=1 python3 fn02_protocol_simulator.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 isolated_component_gates.py
PYTHONDONTWRITEBYTECODE=1 python3 worker_cross_test_launcher.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 "$release_root/tools/test_worker_auto_admission_static.py"

echo "PASS Automation Control Plane E2E v0025 static gates."
