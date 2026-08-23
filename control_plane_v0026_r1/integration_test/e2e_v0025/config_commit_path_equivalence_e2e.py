#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic V9 equivalence gate for both supported config commit origins."""

import base64
import copy
import hashlib
import json
import pathlib
import subprocess
import sys


VERSION = "0.25.0-e2e.3"


def _load(path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _row(config, selector, value):
    section, field = selector
    for index, item in enumerate(config.get(section) or []):
        if isinstance(item, dict) and str(item.get(field)) == str(value):
            return index, item
    raise RuntimeError("{} entity {} is absent".format(section, value))


def _owned_controller(config):
    owned = {
        str(row.get("controller_uuid"))
        for row in config.get("site_controller_map") or []
        if isinstance(row, dict) and str(row.get("node_id")) == "fn-01"
        and str(row.get("routing_role") or "owner") == "owner"
    }
    for index, controller in enumerate(config.get("controllers") or []):
        identity = str(controller.get("controller_uuid") or "") \
            if isinstance(controller, dict) else ""
        sensors = [
            row for row in config.get("sensors") or []
            if isinstance(row, dict) and row.get("controller_uuid") == identity
        ]
        actuators = [
            row for row in config.get("actuators") or []
            if isinstance(row, dict) and row.get("controller_uuid") == identity
        ]
        if identity in owned and sensors and actuators:
            return index, controller, sensors, actuators
    raise RuntimeError("no mutable fn-01 V9 controller exists")


def _pid_state(config, controller_uuid):
    for index, state in enumerate(config.get("process_states") or []):
        refs = state.get("io_refs") if isinstance(state, dict) else None
        strategy = state.get("control_strategy") if isinstance(state, dict) else None
        identities = {
            str(row.get("controller_uuid") or "")
            for section in ("sensors", "actuators")
            for row in (refs or {}).get(section) or []
            if isinstance(row, dict)
        }
        if isinstance(strategy, dict) and strategy.get("type") == "PID" \
                and identities == {str(controller_uuid)} \
                and isinstance(state.get("parameters"), dict):
            return index, state
    raise RuntimeError("no mutable PID state exists")


def _rule_threshold(config, controller_uuid):
    for rule_index, rule in enumerate(config.get("rules") or []):
        if not isinstance(rule, dict) or rule.get("enabled") is False:
            continue
        for condition_index, condition in enumerate(rule.get("when") or []):
            value = condition.get("value") if isinstance(condition, dict) else None
            if isinstance(condition, dict) \
                    and str(condition.get("controller_uuid") or "") \
                    == str(controller_uuid) \
                    and isinstance(value, (int, float)) and not isinstance(value, bool):
                return rule_index, condition_index, value
    raise RuntimeError("no mutable numeric rule threshold exists")


def _alternate(config, selector, current):
    section, field = selector
    for row in config.get(section) or []:
        value = row.get(field) if isinstance(row, dict) else None
        if value is not None and str(value) != str(current):
            return value
    raise RuntimeError("no alternate {} exists".format(field))


def _next(rows, field, minimum):
    used = {
        int(row.get(field)) for row in rows
        if isinstance(row, dict) and isinstance(row.get(field), int)
    }
    value = int(minimum)
    while value in used:
        value += 1
    return value


def _changes(config):
    controller_index, controller, sensors, actuators = _owned_controller(config)
    controller_uuid = str(controller["controller_uuid"])
    state_index, state = _pid_state(config, controller_uuid)
    rule_index, condition_index, threshold = _rule_threshold(config, controller_uuid)
    process_index, process = _row(
        config, ("processes", "p_id"), state.get("p_id"))

    pid_index, pid_config = _row(
        config, ("pid_configs", "pid_config_id"),
        state["control_strategy"]["pid_config_id"])
    sensor = copy.deepcopy(sensors[-1])
    sensor["sensor_id"] = _next(config.get("sensors") or [], "sensor_id", 65000)
    sensor["address"] = _next(sensors, "address", 60)
    sensor["sensor_group"] = "E2E_V9_EQUIVALENCE"
    actuator = copy.deepcopy(actuators[-1])
    actuator["actuator_id"] = _next(config.get("actuators") or [], "actuator_id", 65000)
    actuator["address"] = _next(actuators, "address", 590)
    actuator["actuator_group"] = "E2E_V9_EQUIVALENCE"
    marker = "e2e-v9-equivalence"
    return [{
        "operation": "set",
        "path": "controllers.{}.tags".format(controller_index),
        "value": list(controller.get("tags") or []) + [marker],
    }, {
        "operation": "append", "path": "sensors", "value": sensor,
    }, {
        "operation": "append", "path": "actuators", "value": actuator,
    }, {
        "operation": "set",
        "path": "rules.{}.when.{}.value".format(rule_index, condition_index),
        "value": float(threshold) + 0.25,
    }, {
        "operation": "set",
        "path": "process_states.{}.mode_id".format(state_index),
        "value": _alternate(
            config, ("process_modes", "mode_id"), state.get("mode_id")),
    }, {
        "operation": "set",
        "path": "process_states.{}.value_id".format(state_index),
        "value": _alternate(
            config, ("process_values", "value_id"), state.get("value_id")),
    }, {
        "operation": "set",
        "path": "process_states.{}.parameters.target_temp_c".format(state_index),
        "value": float(state["parameters"]["target_temp_c"]) + 0.75,
    }, {
        "operation": "set",
        "path": "pid_configs.{}.pid_parameters.kp".format(pid_index),
        "value": float(pid_config["pid_parameters"]["kp"]) + 0.001,
    }, {
        "operation": "set",
        "path": "processes.{}.process_location".format(process_index),
        "value": str(process.get("process_location") or "") + "|" + marker,
    }]


def _require_v9(config):
    meta = config.get("meta") or []
    if len(meta) != 1 or meta[0].get("version") != 9:
        raise RuntimeError("global storage is not V9")
    if any("rows" in row for section in ("sensors", "actuators")
           for row in config.get(section) or [] if isinstance(row, dict)):
        raise RuntimeError("global V9 I/O is not flat")


def _verify_worker_interop(worker_root, documents, expected_fingerprint):
    """Apply exact Master operations through the external Worker implementation."""
    before = documents["before"]
    after = documents["after"]
    operations = documents["operations"]
    worker_root = pathlib.Path(worker_root).resolve()
    required = worker_root / "src" / "library" \
        / "_generic_system_contract_library" / "config_sharding_contracts.py"
    if not required.is_file():
        raise RuntimeError("Worker source root is invalid: " + str(worker_root))
    child = r'''
import base64
import hashlib
import json
import pathlib
import sys

worker_root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(worker_root))
from src.library._generic_system_contract_library import scope_diff
from src.library import _generic_type_library as type_library

payload = json.loads(base64.b64decode(
    sys.stdin.buffer.read()).decode("utf-8"))
before = type_library.normalize_config_for_runtime(
    payload["before"], representation="storage")
after = type_library.normalize_config_for_runtime(
    payload["after"], representation="storage")
worker_operations = list(scope_diff.diff_configs(
    before, after, representation="runtime"))
rebuilt = scope_diff.apply_operations(
    before, payload["operations"], representation="runtime")
projected = type_library.project_config_for_storage(rebuilt)

def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def type_mismatches(left, right, path=""):
    if type(left) is not type(right):
        return [path + ":" + type(left).__name__ + "!=" + type(right).__name__]
    if isinstance(left, dict):
        result = []
        for key in sorted(set(left) & set(right)):
            result.extend(type_mismatches(
                left[key], right[key], path + "/" + str(key)))
        return result
    if isinstance(left, list):
        result = []
        for index, pair in enumerate(zip(left, right)):
            result.extend(type_mismatches(
                pair[0], pair[1], path + "/" + str(index)))
        return result
    return []
print(json.dumps({
    "operations_identical": worker_operations == payload["operations"],
    "operations_sha256": hashlib.sha256(canonical(worker_operations)).hexdigest(),
    "storage_identical": canonical(projected) == canonical(payload["after"]),
    "type_mismatches": type_mismatches(projected, payload["after"])[:20],
    "raw_storage_fingerprint": hashlib.sha256(canonical(projected)).hexdigest(),
    "raw_payload_fingerprint": hashlib.sha256(
        canonical(payload["after"])).hexdigest(),
    "target_fingerprint": type_library.config_fingerprint(rebuilt),
    "storage_fingerprint": type_library.config_fingerprint(projected),
    "runtime_status": type_library.verify_runtime_config_data(rebuilt)["status"],
    "storage_status": type_library.verify_storage_config_data(projected)["status"],
}, sort_keys=True))
'''
    payload = json.dumps({
        "before": before,
        "after": after,
        "operations": list(operations),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", child, str(worker_root)],
        input=base64.b64encode(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Worker V9 interop process failed: "
            + completed.stderr.decode("utf-8", errors="replace").strip())
    proof = json.loads(completed.stdout.decode("utf-8"))
    master_operations = json.dumps(
        list(operations), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    master_operations_sha256 = hashlib.sha256(master_operations).hexdigest()
    master_storage = json.dumps(
        after, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    master_raw_storage_sha256 = hashlib.sha256(master_storage).hexdigest()
    if proof.get("operations_identical") is not True \
            or proof.get("operations_sha256") != master_operations_sha256:
        raise RuntimeError("Master and Worker produced different entity operations")
    if proof.get("storage_identical") is not True:
        raise RuntimeError(
            "Worker did not reconstruct the canonical V9 target: "
            + json.dumps(proof, sort_keys=True))
    if {proof.get("target_fingerprint"), proof.get("storage_fingerprint")} \
            != {expected_fingerprint}:
        raise RuntimeError(
            "Master and Worker V9 target fingerprints differ: "
            + json.dumps({
                "master": expected_fingerprint,
                "master_raw_storage": master_raw_storage_sha256,
                "worker_runtime": proof.get("target_fingerprint"),
                "worker_storage": proof.get("storage_fingerprint"),
                "worker_raw_storage": proof.get("raw_storage_fingerprint"),
                "worker_raw_payload": proof.get("raw_payload_fingerprint"),
                "type_mismatches": proof.get("type_mismatches"),
            }, sort_keys=True))
    if proof.get("runtime_status") != "OK" or proof.get("storage_status") != "OK":
        raise RuntimeError("Worker rejected the reconstructed V9 target")
    return {
        "status": "PASSED",
        "operation_count": len(operations),
        "operations_sha256": master_operations_sha256,
        "target_fingerprint": expected_fingerprint,
    }


def _verify_worker_master_precedence(worker_root):
    """Run the pinned Worker's exact Master-vs.-local race regression."""
    resolved = pathlib.Path(worker_root).resolve()
    script = resolved / "test" / "test_config_file_sync.py"
    if not script.is_file() or resolved not in script.parents:
        raise RuntimeError("Worker Master-precedence test is absent")
    completed = subprocess.run(
        [sys.executable, "-B", str(script)],
        cwd=str(resolved),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Worker Master-precedence regression failed: "
            + completed.stderr.decode(
                "utf-8", errors="replace").strip())
    output = completed.stdout.decode("utf-8", errors="replace")
    markers = (
        "master_precedence_one_warning",
        "master_precedence_zero_rollback",
        "stable_error_log_coalescing",
    )
    missing = tuple(marker for marker in markers if marker not in output)
    if missing:
        raise RuntimeError(
            "Worker Master-precedence proof markers are absent: "
            + ",".join(missing))
    return {
        "status": "PASSED",
        "master_wins": True,
        "warning_count": 1,
        "rollback_count": 0,
        "stable_error_stream_coalesced": True,
        "test_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
    }


def _verify_worker_node_local_observation(worker_root, packaged, mirror):
    """Prove that a Master mirror cannot replace node-local Worker truth."""
    child = r'''
import base64
import copy
import json
import pathlib
import sys
import tempfile

worker_root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(worker_root))
from src.core.config_file_sync import (
    _observe_master_baseline,
    create_config_file_sync,
)
from src.library import _generic_type_library as type_library

payload = json.loads(base64.b64decode(
    sys.stdin.buffer.read()).decode("utf-8"))
packaged_storage = payload["packaged"]
mirror_storage = payload["mirror"]
packaged_runtime = type_library.normalize_config_for_runtime(
    packaged_storage, representation="storage")
mirror_runtime = type_library.normalize_config_for_runtime(
    mirror_storage, representation="storage")
packaged_fingerprint = type_library.config_fingerprint(packaged_runtime)
mirror_fingerprint = type_library.config_fingerprint(mirror_runtime)
if packaged_fingerprint == mirror_fingerprint:
    raise RuntimeError("node-local observation fixture does not diverge")


def write_json(path, value):
    path.write_text(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ), encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="fn02-node-local-observation-") as directory:
    root = pathlib.Path(directory)
    config_path = root / "config.json"
    wal_path = root / "proposal-wal.json"
    write_json(config_path, packaged_storage)
    write_json(wal_path, {
        "version": 1,
        "state": "IDLE",
        "node_id": "fn-02",
        "baseline_config": packaged_runtime,
        "baseline_fingerprint": packaged_fingerprint,
        "updated_at_s": 1.0,
    })

    def get_snapshot():
        return copy.deepcopy(mirror_runtime)

    def persist_json(**kwargs):
        write_json(config_path, mirror_storage)
        return {
            "status": "OK",
            "details": {"fingerprint": mirror_fingerprint},
        }

    def distributed_snapshot():
        return {
            "node_shard_fingerprint": mirror_fingerprint,
        }

    api = create_config_file_sync(
        options={
            "enabled": True,
            "node_id": "fn-02",
            "config_path": str(config_path),
            "wal_path": str(wal_path),
            "config_authority": "node_local",
        },
        config_capabilities={
            "get_snapshot": get_snapshot,
            "persist_json": persist_json,
        },
        distributed_api={"snapshot": distributed_snapshot},
    )
    context = api["context"]
    observed = _observe_master_baseline(context)
    materialized = json.loads(config_path.read_text(encoding="utf-8"))
    statistics = context["statistics"]
    print(json.dumps({
        "observed": observed,
        "wal_state": context["wal"].get("state"),
        "baseline_matches_packaged": context["wal"].get("baseline_fingerprint")
            == packaged_fingerprint,
        "baseline_matches_mirror": context["wal"].get("baseline_fingerprint")
            == mirror_fingerprint,
        "materialized_packaged_exact": materialized == packaged_storage,
        "materialized_mirror_exact": materialized == mirror_storage,
        "proposals": statistics.get("proposals"),
        "local_first": statistics.get("local_first"),
        "master_first": statistics.get("master_first"),
        "materializations": statistics.get("materializations"),
        "packaged_fingerprint": packaged_fingerprint,
        "mirror_fingerprint": mirror_fingerprint,
    }, sort_keys=True))
'''
    payload = json.dumps({
        "packaged": packaged,
        "mirror": mirror,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", child, str(pathlib.Path(worker_root).resolve())],
        input=base64.b64encode(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Worker node-local mirror observation failed: "
            + completed.stderr.decode("utf-8", errors="replace").strip())
    proof = json.loads(completed.stdout.decode("utf-8"))
    expected = {
        "observed": False,
        "wal_state": "IDLE",
        "baseline_matches_packaged": True,
        "baseline_matches_mirror": False,
        "materialized_packaged_exact": True,
        "materialized_mirror_exact": False,
        "proposals": 0,
        "local_first": 0,
        "master_first": 0,
        "materializations": 0,
    }
    if any(proof.get(key) != value for key, value in expected.items()):
        raise RuntimeError(
            "node-local Worker accepted a Master mirror as authoritative: "
            + json.dumps(proof, sort_keys=True))
    return {
        "status": "PASSED",
        "worker_fingerprint": proof["packaged_fingerprint"],
        "mirror_fingerprint": proof["mirror_fingerprint"],
        "master_materialized": False,
        "proposals": 0,
    }

def main(argv=None):
    worker_root = None
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--worker-root":
            raise RuntimeError("usage: config_commit_path_equivalence_e2e.py [--worker-root PATH]")
        worker_root = arguments[1]
    release = pathlib.Path(__file__).resolve().parents[2]
    master = release / "master_node" / "master_node"
    if str(master) not in sys.path:
        sys.path.insert(0, str(master))

    from src.library._generic_system_contract_library import (
        config_graph,
        scope_diff,
        shard_library,
        type_library,
    )
    from src.library._generic_type_library import fingerprint
    from src.core import config_authority

    path = release / "master_node" / "master_node" / "_config" \
        / "application" / "initial_config_v9.json"
    before = _load(path)
    _require_v9(before)
    changes = _changes(before)
    native = config_authority.apply_changes(
        before, changes,
        allowed_prefixes=config_authority.DEFAULT_ALLOWED_PREFIXES,
    )
    if native.get("ok") is not True:
        raise RuntimeError("native path change set was rejected")
    candidate = native["config"]
    _require_v9(candidate)
    report = type_library.verify_config_data(candidate)
    graph = config_graph.validate_authority_config(candidate, profile="lab_v2")
    if report.get("status") != "OK" or graph.get("ok") is not True:
        raise RuntimeError("native V9 candidate failed validation")

    file_operations = scope_diff.diff_configs(before, candidate)
    rebuilt = scope_diff.apply_operations(before, file_operations)
    if fingerprint(rebuilt) != fingerprint(candidate):
        raise RuntimeError("File-WAL and native diff materialize differently")

    restored = scope_diff.apply_operations(
        rebuilt, scope_diff.diff_configs(rebuilt, before))
    if fingerprint(restored) != fingerprint(before):
        raise RuntimeError("differential restore differs from V9 baseline")

    before_node = shard_library.build_local_execution_shard(before, "fn-01")
    after_node = shard_library.build_local_execution_shard(candidate, "fn-01")
    before_cross = shard_library.build_cross_execution_shard(before, "fn-01")
    after_cross = shard_library.build_cross_execution_shard(candidate, "fn-01")
    _require_v9(before_node)
    _require_v9(after_node)
    cross_meta = after_cross.get("meta") or []
    if len(cross_meta) != 1 or cross_meta[0].get("version") != 8 \
            or cross_meta[0].get("scope") != "cross":
        raise RuntimeError("Cross shard is not the separate V8 contract")
    if fingerprint(before_node) == fingerprint(after_node):
        raise RuntimeError("runtime matrix did not change the Node shard")
    if fingerprint(before_cross) == fingerprint(after_cross):
        raise RuntimeError("shared PID catalog did not change the Cross shard")

    worker_interop = None
    worker_fn02_bootstrap_interop = None
    worker_master_precedence = None
    if worker_root is not None:
        node_operations = scope_diff.diff_configs(before_node, after_node)
        worker_master_precedence = _verify_worker_master_precedence(worker_root)
        worker_interop = _verify_worker_interop(worker_root, {
            "before": before_node,
            "after": after_node,
            "operations": node_operations,
        }, type_library.config_fingerprint(after_node))

        worker_fn02_path = pathlib.Path(worker_root) / "_config" \
            / "config_data" / "device_state_data_v9_node_fn-02.json"
        worker_fn02 = _load(worker_fn02_path)
        master_fn02 = shard_library.build_local_execution_shard(before, "fn-02")
        _require_v9(worker_fn02)
        _require_v9(master_fn02)
        worker_fn02_meta = worker_fn02.get("meta") or []
        master_fn02_meta = master_fn02.get("meta") or []
        if worker_fn02_meta[0].get("node_id") != "fn-02" \
                or master_fn02_meta[0].get("node_id") != "fn-02":
            raise RuntimeError("fn-02 bootstrap or Master shard has wrong identity")
        fn02_operations = scope_diff.diff_configs(worker_fn02, master_fn02)
        worker_fn02_bootstrap_interop = _verify_worker_interop(worker_root, {
            "before": worker_fn02,
            "after": master_fn02,
            "operations": fn02_operations,
        }, type_library.config_fingerprint(master_fn02))
        worker_fn02_bootstrap_interop["node_local_master_observation"] = \
            _verify_worker_node_local_observation(
                worker_root, worker_fn02, master_fn02)

    result = {
        "status": "PASSED",
        "version": VERSION,
        "primary_storage_version": 9,
        "cross_storage_version": 8,
        "native_change_count": len(changes),
        "file_operation_count": len(file_operations),
        "origins": [
            "local_json_file_wal_diff",
            "native_client_proxy_mqtt_master_diff",
        ],
        "candidate_fingerprint": fingerprint(candidate),
        "restored_fingerprint": fingerprint(restored),
    }
    if worker_interop is not None:
        result["worker_interop"] = worker_interop
        result["worker_fn02_bootstrap_interop"] = worker_fn02_bootstrap_interop
        result["worker_master_precedence"] = worker_master_precedence
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
