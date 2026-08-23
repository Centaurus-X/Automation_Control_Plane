#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for the fail-closed Authority-V5 Worker file verifier."""

import copy
import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile

from pathlib import Path


def _worker_script(root):
    staged = root / "worker_config_file_e2e.py"
    if staged.is_file():
        return staged
    return root.parent / "integration_test" / "e2e_v0025" \
        / "worker_config_file_e2e.py"


def _load_worker_module(root, name):
    script = _worker_script(root)
    specification = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _identity(value):
    return copy.deepcopy(value)


def _controller_selector_regression(root):
    sys.dont_write_bytecode = True
    module = _load_worker_module(root, "worker_file_controller_test")
    config = {
        "site_controller_map": [
            {"controller_uuid": "empty", "node_id": "fn-01",
             "routing_role": "owner"},
            {"controller_uuid": "actuator-524", "node_id": "fn-01",
             "routing_role": "owner"},
        ],
        "controllers": [
            {"controller_uuid": "empty", "tags": ["first"]},
            {"controller_uuid": "actuator-524", "tags": ["io"]},
        ],
    }
    selected = module._owned_controller(config, "fn-01", "actuator-524")
    assert selected["controller_uuid"] == "actuator-524"


def _differential_payload_regression(root):
    module = _load_worker_module(root, "worker_file_payload_test")
    cases = (
        ("policy_fallback", "scope_operations", "operations"),
        ("structural", "scope_operations", "operations"),
        ("runtime", "action_plan", "actions"),
    )
    for route, operation, item_key in cases:
        payload = {"operation": operation, item_key: [{"path": "rules.0"}]}
        actual_operation, items = module._differential_payload_items(
            payload, route)
        assert actual_operation == operation
        assert items == payload[item_key]
        try:
            module._differential_payload_items(
                {"operation": operation, item_key: []}, route)
        except RuntimeError:
            pass
        else:
            raise AssertionError(route + " accepted an empty payload")


def _create_authority_database(path, module):
    operation_id = "cce259be-d2e1-4db7-bbe6-f8c7a00d2840"
    relay_message_id = "relay:" + operation_id
    mirror_id = "40000000-0000-4000-8000-000000000005"
    record_id = "30000000-0000-4000-8000-000000000005"
    snapshot_config = {"proof": "node-local"}
    fingerprint = module._fingerprint(snapshot_config)
    expected = {
        "operation_id": operation_id,
        "relay_message_id": relay_message_id,
        "authority_epoch": 0,
        "base_scope_revision": 4,
        "target_scope_revision": 5,
        "target_scope_fingerprint": fingerprint,
        "node_replica_revision": 5,
        "node_replica_fingerprint": fingerprint,
        "operation_format": "config_scope_operations_v1",
        "operation_count": 6,
    }
    post_commit = {
        "ok": True,
        "status": "node_authoritative_committed",
        "authority_epoch": 0,
        "scope_revision": 5,
        "fingerprint": fingerprint,
        "mirror_id": mirror_id,
    }
    result = {
        "ok": True,
        "code": "accepted",
        "status": "accepted",
        "operation_id": operation_id,
        "principal": "master_relay",
        "authority_epoch": 0,
        "scope_revision": 5,
        "fingerprint": fingerprint,
        "post_commit": post_commit,
    }
    journal = {
        "format": "authority_journal_v1",
        "record_id": record_id,
        "kind": "scope_commit",
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "scope_revision": 5,
        "execution_epoch": 0,
        "fingerprint": fingerprint,
        "created_at_s": 1.0,
        "payload": {
            "event_type": "scope_operations",
            "operation_id": operation_id,
            "principal": "master_relay",
            "source": "external_proposal",
            "sem_revision": 8,
        },
    }
    snapshot = {
        "format": "authority_snapshot_v1",
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "scope_revision": 5,
        "execution_epoch": 0,
        "fingerprint": fingerprint,
        "config": snapshot_config,
    }
    mirror = {
        "format": "worker_mirror_v1",
        "mirror_id": mirror_id,
        "command": "distributed.mirror.snapshot",
        "state": "pending",
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "scope_revision": 5,
        "execution_epoch": 0,
        "fingerprint": fingerprint,
        "snapshot": snapshot,
        "attempts": 0,
        "created_at_s": 1.0,
        "updated_at_s": 1.0,
    }
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE inbox(message_id TEXT PRIMARY KEY,command TEXT," 
        "payload_hash TEXT,state TEXT,result_json TEXT,created_at REAL," 
        "updated_at REAL)")
    connection.execute(
        "CREATE TABLE authority_journal(record_id TEXT PRIMARY KEY," 
        "kind TEXT,scope TEXT,authority_epoch INTEGER,scope_revision INTEGER," 
        "execution_epoch INTEGER,fingerprint TEXT,record_json TEXT," 
        "created_at REAL)")
    connection.execute(
        "CREATE TABLE mirror_outbox(mirror_id TEXT PRIMARY KEY,scope TEXT," 
        "authority_epoch INTEGER,scope_revision INTEGER,fingerprint TEXT," 
        "state TEXT,attempts INTEGER,record_json TEXT,next_attempt_at REAL," 
        "created_at REAL,updated_at REAL)")
    connection.execute(
        "INSERT INTO inbox VALUES(?,?,?,?,?,?,?)",
        (relay_message_id, "distributed.config.propose", "e" * 64,
         "DONE", json.dumps(result), 1.0, 2.0))
    connection.execute(
        "INSERT INTO authority_journal VALUES(?,?,?,?,?,?,?,?,?)",
        (record_id, "scope_commit", "node:fn-01", 0, 5, 0,
         fingerprint, json.dumps(journal), 1.0))
    connection.execute(
        "INSERT INTO mirror_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (mirror_id, "node:fn-01", 0, 5, fingerprint, "acked", 1,
         json.dumps(mirror), 1.0, 1.0, 2.0))
    connection.commit()
    connection.close()
    return expected


def _node_local_inbox_correlation_regression(root):
    module = _load_worker_module(root, "worker_file_inbox_test")
    module._PROJECT_STORAGE = _identity
    with tempfile.TemporaryDirectory(
            prefix="node-local-proposal-inbox-") as directory:
        database = Path(directory) / "operations.sqlite3"
        expected = _create_authority_database(database, module)
        accepted = module._node_local_relay_proof(
            database, expected, "fn-01")
        assert accepted["operation_id"] == expected["operation_id"]
        assert accepted["mirror_state"] == "acked"
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE mirror_outbox SET state='pending'")
        connection.commit()
        connection.close()
        try:
            module._node_local_relay_proof(database, expected, "fn-01")
        except RuntimeError:
            pass
        else:
            raise AssertionError("unacknowledged Core mirror was accepted")
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE mirror_outbox SET state='acked'")
        connection.execute(
            "UPDATE inbox SET payload_hash='invalid'")
        connection.commit()
        connection.close()
        try:
            module._node_local_relay_proof(database, expected, "fn-01")
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid Core inbox payload hash was accepted")


def _upsert(section, payload, entity_key=None):
    keys = {
        "controllers": "controller:ctrl-wago-1",
        "sensors": "sensor:ctrl-wago-1:60000",
        "actuators": "actuator:ctrl-wago-1:60000",
        "rules": "rule:1",
        "process_states": "state:21",
        "processes": "process:1",
    }
    identity = entity_key or keys[section]
    return {
        "op": "upsert_entity",
        "section": section,
        "entity_key": identity,
        "expected_value_hash": "1" * 64,
        "value_hash": "2" * 64,
        "value": {
            "section": section,
            "entity_key": identity,
            "position": 0,
            "payload": payload,
        },
    }


def _phase_items(phase):
    if phase in ("policy_candidate", "policy_restore"):
        target = 400.0 if phase == "policy_candidate" else 200.0
        return [_upsert("process_states", {
            "state_id": 21,
            "mode_id": 1,
            "value_id": 1,
            "parameters": {"target_temp_c": target},
        })]
    if phase == "structural_candidate":
        return [
            _upsert("controllers", {
                "controller_uuid": "ctrl-wago-1",
                "tags": ["baseline", "worker-file-e2e-proof"],
            }),
            _upsert("sensors", {
                "controller_uuid": "ctrl-wago-1",
                "sensor_id": 60000,
                "address": 14,
            }),
            _upsert("actuators", {
                "controller_uuid": "ctrl-wago-1",
                "actuator_id": 60000,
                "address": 547,
            }),
            _upsert("rules", {
                "rule_id": 1,
                "enabled": False,
                "when": [{"value": 540.5}],
            }),
            _upsert("process_states", {
                "state_id": 21,
                "mode_id": 2,
                "value_id": 1,
                "parameters": {"target_temp_c": 200.0},
            }),
            _upsert("process_states", {
                "state_id": 1,
                "mode_id": 1,
                "value_id": 2,
                "parameters": {},
            }, "state:1"),
            _upsert("processes", {
                "p_id": 1,
                "process_location": "lab|e2e",
            }),
        ]
    if phase == "structural_restore":
        return [
            _upsert("controllers", {
                "controller_uuid": "ctrl-wago-1",
                "tags": ["baseline"],
            }),
            {"op": "delete_entity", "section": "sensors",
             "entity_key": "sensor:ctrl-wago-1:60000",
             "expected_value_hash": "1" * 64},
            {"op": "delete_entity", "section": "actuators",
             "entity_key": "actuator:ctrl-wago-1:60000",
             "expected_value_hash": "1" * 64},
            _upsert("rules", {
                "rule_id": 1,
                "enabled": True,
                "when": [{"value": 540.0}],
            }),
            _upsert("process_states", {
                "state_id": 21,
                "mode_id": 1,
                "value_id": 1,
                "parameters": {"target_temp_c": 200.0},
            }),
            _upsert("process_states", {
                "state_id": 1,
                "mode_id": 1,
                "value_id": 1,
                "parameters": {},
            }, "state:1"),
            _upsert("processes", {
                "p_id": 1,
                "process_location": "lab",
            }),
        ]
    if phase == "runtime_candidate":
        return [
            {"state_ids": [1], "value_id": 1},
            {"state_ids": [21], "value_id": 2,
             "parameters": {"target_temp_c": 400.0}},
        ]
    return [
        {"state_ids": [1], "value_id": 2},
        {"state_ids": [21], "value_id": 1,
         "parameters": {"target_temp_c": 200.0}},
    ]


def _items_hash(items):
    return hashlib.sha256(json.dumps(
        items, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _transitions():
    phases = (
        ("policy_candidate", "policy_fallback"),
        ("policy_restore", "policy_fallback"),
        ("structural_candidate", "structural"),
        ("runtime_candidate", "runtime"),
        ("runtime_restore", "runtime"),
        ("structural_restore", "structural"),
    )
    fingerprints = (
        "b" * 64, "a" * 64, "c" * 64,
        "d" * 64, "c" * 64, "a" * 64,
    )
    previous_fingerprint = "a" * 64
    result = []
    for revision, values, fingerprint in zip(
            range(13, 19), phases, fingerprints):
        phase, route = values
        items = _phase_items(phase)
        operation = "action_plan" if route == "runtime" \
            else "scope_operations"
        operation_id = "file:fn-01:proof:{}".format(revision)
        commit_id = operation_id + ":scope-fallback" \
            if route == "policy_fallback" else operation_id
        items_hash = _items_hash(items)
        result.append({
            "phase": phase,
            "authority_scope": "worker_node_local",
            "authority_epoch": 0,
            "base_scope_revision": revision - 1,
            "scope_revision": revision,
            "fingerprint": fingerprint,
            "operation_id": operation_id,
            "commit_operation_id": commit_id,
            "mirror_id": "20000000-0000-4000-8000-{:012d}".format(
                revision),
            "mirror_state": "acked",
            "mirror_attempts": 1,
            "observed_modes": ["node_local"],
            "semantic_postcondition": True,
            "local_commit_ok": True,
            "authority_journal_correlated": True,
            "journal_record_id": 
                "10000000-0000-4000-8000-{:012d}".format(revision),
            "journal_format": "authority_journal_v1",
            "mirror_ack_correlated": True,
            "mirror_format": "worker_mirror_v1",
            "commit_origin": "local_json_file_sem_node_authority",
            "file_route": route,
            "file_wal_mode": "node_local",
            "file_commit_kind": operation,
            "wal_authority_correlated": True,
            "wal_baseline_fingerprint": previous_fingerprint,
            "wal_candidate_fingerprint": fingerprint,
            "differential_operation": operation,
            "differential_operation_count": len(items),
            "differential_items": items,
            "differential_items_sha256": items_hash,
            "full_scope_operations_sha256": items_hash,
            "target_scope": "node:fn-01",
            "target_scope_revision": revision,
            "target_scope_fingerprint": fingerprint,
        })
        previous_fingerprint = fingerprint
    return result


def _managed_projection():
    return {
        "state_1_value_id": 1,
        "state_1_mode_id": 1,
        "rule_1_enabled": True,
        "rule_1_threshold": 540.0,
        "pid_state_id": 21,
        "pid_mode_id": 1,
        "pid_value_id": 1,
        "pid_target_temp_c": 200.0,
        "process_id": 1,
        "process_location": "lab",
        "controller_tags": ["baseline"],
        "marker_present": False,
        "sensor_present": False,
        "actuator_present": False,
    }


def _proof():
    transitions = _transitions()
    operation_id = "cce259be-d2e1-4db7-bbe6-f8c7a00d2840"
    managed = _managed_projection()
    return {
        "status": "PASSED",
        "version": "0.25.0-e2e.3",
        "worker_version": "automation_system_v0101",
        "node_id": "fn-01",
        "original_fingerprint": "a" * 64,
        "final_fingerprint": "a" * 64,
        "restore_confirmed": True,
        "core_node_local_proposal": {
            "commit_origin": 
                "canonical_client_proxy_master_relay_worker_local",
            "transport_command": "distributed.config.propose",
            "transport_mode": "canonical_scope_proposal",
            "operation_id": operation_id,
            "relay_message_id": "relay:" + operation_id,
            "operation_format": "config_scope_operations_v1",
            "operation_count": 6,
            "authority_epoch": 0,
            "base_scope_revision": 9,
            "target_scope": "node:fn-01",
            "target_scope_revision": 10,
            "target_scope_fingerprint": "9" * 64,
            "node_replica_revision": 10,
            "node_replica_fingerprint": "9" * 64,
            "worker_inbox_state": "DONE",
            "worker_payload_sha256": "e" * 64,
            "journal_record_id": 
                "30000000-0000-4000-8000-000000000010",
            "authority_journal_correlated": True,
            "mirror_id": "40000000-0000-4000-8000-000000000010",
            "mirror_state": "acked",
            "mirror_ack_correlated": True,
            "current_run_correlated": True,
            "worker_applied": True,
        },
        "managed_original": managed,
        "managed_final": copy.deepcopy(managed),
        "config_matrix": {
            "commit_paths": ["worker_node_local"],
            "commit_origin": "local_json_file_sem_node_authority",
            "master_mirror_is_non_authoritative": True,
            "authority_journal_required": True,
            "exact_mirror_ack_required": True,
            "operator_file_policy_fallback": {
                "state_id": 21,
                "safety_code": "required_mode",
                "active_mode_id": 1,
                "required_mode_id": 2,
                "candidate_target_temp_c": 400.0,
                "authority_scope": "worker_node_local",
                "restored": True,
            },
            "new_sensor": {
                "controller_uuid": "ctrl-wago-1",
                "sensor_id": 60000,
                "address": 14,
                "added": True,
                "restored": True,
            },
            "new_actuator": {
                "controller_uuid": "ctrl-wago-1",
                "actuator_id": 60000,
                "address": 547,
                "added": True,
                "restored": True,
            },
            "pid_state": {
                "state_id": 21,
                "mode_id": {"before": 1, "after": 2},
                "value_id": {"before": 1, "after": 2},
                "target_temp_c": {"before": 200.0, "after": 400.0},
            },
            "rule_threshold": {"before": 540.0, "after": 540.5},
            "process_data": {
                "p_id": 1,
                "process_location": {"before": "lab", "after": "lab|e2e"},
            },
            "storage_contract": {
                "version": 9,
                "io_layout": "flat",
                "primary": True,
            },
            "worker_live_ack": True,
            "restore_confirmed": True,
        },
        "recovery_journal": {
            "journal_version": 3,
            "status": "COMPLETED",
            "separate_from_product_file_wal": True,
            "prepared_before_first_mutation": True,
            "full_v9_baseline_recorded": True,
            "selectors_recorded": True,
            "physical_actuator_baseline_recorded": True,
            "physical_actuator_restore_confirmed": True,
            "baseline_fingerprint": "a" * 64,
            "final_fingerprint": "a" * 64,
            "restore_scope_revision": 18,
            "restore_mirror_state": "acked",
            "restore_authority_journal_correlated": True,
            "restore_mirror_ack_correlated": True,
            "restore_confirmed": True,
            "previous_open_journal_recovered": False,
        },
        "actuator_524": {
            "actuator_id": 3,
            "address": 524,
            "readback_verified": True,
            "new_log_evidence": True,
            "baseline_value": 7,
            "observed_before_restore": 9,
            "restored_value": 7,
            "restore_readback_verified": True,
            "write_performed": True,
            "verified_noop": False,
            "approved_endpoint": "192.168.0.3:5020",
        },
        "transitions": transitions,
    }


def _run(verifier, path):
    return subprocess.run(
        [sys.executable, str(verifier), "--result", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _write_and_reject(verifier, path, value, label):
    path.write_text(json.dumps(value), encoding="utf-8")
    result = _run(verifier, path)
    assert result.returncode != 0, label + " was accepted"


def _negative_contract_regressions(verifier, path):
    cases = []
    value = _proof()
    value["core_node_local_proposal"]["operation_count"] = 0
    cases.append(("empty Core operation set", value))
    value = _proof()
    value["core_node_local_proposal"]["relay_message_id"] = "relay:foreign"
    cases.append(("foreign Core relay", value))
    value = _proof()
    value["core_node_local_proposal"]["target_scope_revision"] = 11
    cases.append(("non-monotonic Core revision", value))
    value = _proof()
    value["transitions"][0]["base_scope_revision"] = 9
    cases.append(("historical file transition", value))
    value = _proof()
    value["transitions"][1]["base_scope_revision"] = 99
    cases.append(("non-contiguous file transition", value))
    value = _proof()
    value["transitions"][2]["authority_epoch"] = 1
    cases.append(("foreign authority epoch", value))
    value = _proof()
    value["transitions"][0]["commit_operation_id"] = \
        value["transitions"][0]["operation_id"]
    cases.append(("missing policy fallback identity", value))
    value = _proof()
    value["transitions"][3]["differential_operation"] = "scope_operations"
    cases.append(("wrong runtime differential", value))
    value = _proof()
    value["transitions"][0]["differential_items_sha256"] = "f" * 64
    cases.append(("wrong differential hash", value))
    value = _proof()
    value["transitions"][0]["file_route"] = "structural"
    cases.append(("wrong file route", value))
    value = _proof()
    value["transitions"][2]["authority_journal_correlated"] = False
    cases.append(("missing journal correlation", value))
    value = _proof()
    value["transitions"][2]["mirror_state"] = "pending"
    cases.append(("missing mirror ACK", value))
    value = _proof()
    value["transitions"][2]["mirror_id"] = \
        value["transitions"][1]["mirror_id"]
    cases.append(("duplicate mirror identity", value))
    value = _proof()
    value["transitions"][4]["wal_baseline_fingerprint"] = "f" * 64
    cases.append(("broken WAL fingerprint chain", value))
    value = _proof()
    value["final_fingerprint"] = "f" * 64
    cases.append(("final fingerprint drift", value))
    value = _proof()
    value["managed_final"]["state_1_value_id"] = 999
    cases.append(("managed-state drift", value))
    value = _proof()
    items = value["transitions"][2]["differential_items"]
    items[1]["value"]["payload"]["sensor_id"] = 60001
    value["transitions"][2]["differential_items_sha256"] = _items_hash(items)
    cases.append(("arbitrary structural entity", value))
    value = _proof()
    items = value["transitions"][3]["differential_items"]
    items[0]["value_id"] = 2
    value["transitions"][3]["differential_items_sha256"] = _items_hash(items)
    cases.append(("wrong runtime phase", value))
    value = _proof()
    value["recovery_journal"]["restore_scope_revision"] = 13
    cases.append(("stale recovery revision", value))
    value = _proof()
    value["recovery_journal"]["restore_mirror_state"] = "pending"
    cases.append(("unacknowledged recovery mirror", value))
    value = _proof()
    value["config_matrix"]["operator_file_policy_fallback"][
        "authority_scope"] = "worker_file_master_first"
    cases.append(("legacy policy authority", value))
    for label, candidate in cases:
        _write_and_reject(verifier, path, candidate, label)


def main():
    root = Path(__file__).resolve().parent
    _controller_selector_regression(root)
    _differential_payload_regression(root)
    _node_local_inbox_correlation_regression(root)
    verifier = root / "verify_worker_config_file_e2e.py"
    with tempfile.TemporaryDirectory(
            prefix="worker-file-e2e-verifier-") as directory:
        path = Path(directory) / "result.json"
        valid = _proof()
        path.write_text(json.dumps(valid), encoding="utf-8")
        accepted = _run(verifier, path)
        assert accepted.returncode == 0, accepted.stderr
        _negative_contract_regressions(verifier, path)
    print("PASS Authority-V5 Worker config-file E2E result verifier")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())