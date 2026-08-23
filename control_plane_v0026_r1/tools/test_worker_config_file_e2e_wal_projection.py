#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regress the real grouped Worker WAL to canonical flat-V9 proof boundary."""

import argparse
import copy
import importlib.util
import json
import sys

from pathlib import Path


def _states_by_id(config):
    return {
        str(row.get("state_id")): row
        for row in config.get("process_states") or ()
        if isinstance(row, dict) and row.get("state_id") is not None
    }


def _runtime_action(before, after):
    prior = copy.deepcopy(before)
    target = copy.deepcopy(after)
    prior_parameters = prior.pop("parameters", {})
    target_parameters = target.pop("parameters", {})
    prior_value = prior.pop("value_id", None)
    target_value = target.pop("value_id", None)
    if prior != target \
            or not isinstance(prior_parameters, dict) \
            or not isinstance(target_parameters, dict) \
            or set(prior_parameters) - set(target_parameters):
        return None
    changed_parameters = {
        key: copy.deepcopy(target_parameters[key])
        for key in sorted(target_parameters)
        if key not in prior_parameters
        or prior_parameters[key] != target_parameters[key]
    }
    action = {"state_ids": [copy.deepcopy(after.get("state_id"))]}
    if prior_value != target_value:
        action["value_id"] = copy.deepcopy(target_value)
    if changed_parameters:
        action["parameters"] = changed_parameters
    return action if len(action) > 1 else None


def _local_runtime_actions(before, after, operations):
    """Independent mirror of the bounded Worker runtime-only classifier."""
    if not operations:
        return ()
    if any(operation.get("section") != "process_states"
           for operation in operations):
        return None
    prior = _states_by_id(before)
    target = _states_by_id(after)
    if set(prior) != set(target):
        return None
    actions = []
    for state_id in sorted(prior):
        if prior[state_id] == target[state_id]:
            continue
        action = _runtime_action(prior[state_id], target[state_id])
        if action is None:
            return None
        actions.append(action)
    return tuple(actions) if actions else None



def _runtime_overlay_matches(live, candidate, actions):
    if not isinstance(actions, list) or not actions:
        return False
    live_states = _states_by_id(live)
    candidate_states = _states_by_id(candidate)
    for action in actions:
        state_ids = action.get("state_ids") if isinstance(action, dict) else None
        if not isinstance(state_ids, list) or len(state_ids) != 1:
            return False
        identity = str(state_ids[0])
        live_state = live_states.get(identity)
        candidate_state = candidate_states.get(identity)
        if not isinstance(live_state, dict) or not isinstance(
                candidate_state, dict):
            return False
        if "value_id" in action \
                and live_state.get("value_id") != candidate_state.get("value_id"):
            return False
        for key in (action.get("parameters") or {}):
            if (live_state.get("parameters") or {}).get(key) \
                    != (candidate_state.get("parameters") or {}).get(key):
                return False
    return True
def _load_e2e(release_root):
    sys.dont_write_bytecode = True
    script = release_root / "integration_test" / "e2e_v0025" \
        / "worker_config_file_e2e.py"
    specification = importlib.util.spec_from_file_location(
        "worker_config_file_e2e_wal_projection_test", script)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _bind_release_contract(module, release_root):
    """Bind the packaged canonical contract for hermetic archive validation."""
    master_root = release_root / "master_node" / "master_node"
    source_text = str(master_root.resolve())
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from src.library._generic_system_contract_library import (
        config_compatibility,
        scope_diff,
        type_library,
    )

    def runtime_scope_diff(before, after):
        # The packaged Master registry deliberately accepts canonical flat
        # storage only. Project the grouped File-WAL snapshots before using
        # that registry; the external-worker mode below executes its native
        # grouped registry directly.
        return scope_diff.diff_configs(
            config_compatibility.project_config_for_storage(before),
            config_compatibility.project_config_for_storage(after),
        )

    module._STORAGE_VERIFIER = type_library.verify_config_data
    module._RUNTIME_VERIFIER = type_library.verify_runtime_config_data
    module._PROJECT_STORAGE = config_compatibility.project_config_for_storage
    module._NORMALIZE_RUNTIME = config_compatibility.normalize_config_for_runtime
    module._SCOPE_DIFF = runtime_scope_diff
    module._LOCAL_RUNTIME_ACTIONS = _local_runtime_actions
    module._RUNTIME_OVERLAY_MATCHES = _runtime_overlay_matches


def _expect_rejected(module, evidence, authority):
    try:
        module._validated_wal_evidence(evidence["wal"], {
            "baseline": evidence["baseline"],
            "target": evidence["target"],
            "authority": authority,
        })
    except RuntimeError:
        return
    raise AssertionError("invalid terminal grouped-WAL proof was accepted")


def _wal(module, configs, route):
    baseline_runtime = configs["baseline"]
    candidate_runtime = configs["candidate"]
    baseline_storage = module._PROJECT_STORAGE(baseline_runtime)
    candidate_storage = module._PROJECT_STORAGE(candidate_runtime)
    operations = list(module._SCOPE_DIFF(
        baseline_runtime, candidate_runtime))
    actions = module._LOCAL_RUNTIME_ACTIONS(
        baseline_runtime, candidate_runtime, operations)
    action_items = list(actions or ())
    operation_id = "file:fn-01:grouped:wal:projection:" + route
    commit_kind = "action_plan" if route == "runtime" \
        else "scope_operations"
    commit_operation_id = operation_id + ":scope-fallback" \
        if route == "policy_fallback" else operation_id
    candidate_fingerprint = module._fingerprint(candidate_storage)
    mirror_id = "20000000-0000-4000-8000-000000000011"
    post_commit = {
        "ok": True,
        "status": "node_authoritative_committed",
        "authority_epoch": 0,
        "scope_revision": 11,
        "fingerprint": candidate_fingerprint,
        "mirror_id": mirror_id,
    }
    sem_result = {
        "ok": True,
        "changed": True,
        "operation_id": commit_operation_id,
        "post_commit": post_commit,
    }
    authority_commit = {
        "authority_mode": "node_local",
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "observed_base_scope_revision": 10,
        "scope_revision": 11,
        "fingerprint": candidate_fingerprint,
        "mirror_id": mirror_id,
        "status": "node_authoritative_committed",
        "changed": True,
        "commit_kind": commit_kind,
    }
    wal = {
        "state": "IDLE",
        "baseline_config": copy.deepcopy(candidate_runtime),
        "baseline_fingerprint": candidate_fingerprint,
        "last_result": copy.deepcopy(sem_result),
        "last_operation": {
            "operation_id": operation_id,
            "mode": "node_local",
            "commit_kind": commit_kind,
            "baseline_fingerprint": module._fingerprint(baseline_storage),
            "candidate_fingerprint": candidate_fingerprint,
            "operations": copy.deepcopy(operations),
            "actions": copy.deepcopy(action_items),
            "scope_baseline": {
                "scope": "node:fn-01",
                "authority_epoch": 0,
                "revision": 10,
                "fingerprint": module._fingerprint(baseline_storage),
            },
            "authority_commit": authority_commit,
            "sem_result": sem_result,
        },
    }
    return wal, baseline_storage, candidate_storage, actions

def _alternate_value(config, current):
    for row in config.get("process_values") or ():
        if isinstance(row, dict) and row.get("value_id") != current:
            return row.get("value_id")
    raise RuntimeError("real Worker fixture has no alternate process value")


def _drift_first_sensor(runtime_config):
    for group in runtime_config.get("sensors") or ():
        rows = group.get("rows") if isinstance(group, dict) else None
        if isinstance(rows, list) and rows:
            rows[0]["address"] += 1
            return
    raise RuntimeError("real Worker fixture has no grouped sensor row")


def _assert_transition_route_contract(module, release_root):
    expected = (
        ("policy_candidate", "worker_node_local", "policy_fallback"),
        ("policy_restore", "worker_node_local", "policy_fallback"),
        ("structural_candidate", "worker_node_local", "structural"),
        ("runtime_candidate", "worker_node_local", "runtime"),
        ("runtime_restore", "worker_node_local", "runtime"),
        ("structural_restore", "worker_node_local", "structural"),
    )
    if module.EXPECTED_TRANSITIONS != expected:
        raise AssertionError("Worker file-E2E phase/route contract differs")
    for transition in expected:
        route = module._required_transition_route({
            "expected_file_route": transition[2],
        })
        if route != transition[2]:
            raise AssertionError("Worker file-E2E route binding differs")
    for invalid in ({}, {"expected_file_route": ""},
                    {"expected_file_route": "unknown"}):
        try:
            module._required_transition_route(invalid)
        except RuntimeError:
            continue
        raise AssertionError("implicit Worker file-E2E route was accepted")
    script = release_root / "integration_test" / "e2e_v0025" \
        / "worker_config_file_e2e.py"
    source = script.read_text(encoding="utf-8")
    for index in range(len(expected)):
        binding = '"expected_file_route": EXPECTED_TRANSITIONS[{}][2],'.format(
            index)
        if source.count(binding) != 1:
            raise AssertionError(
                "Worker file-E2E phase {} has no unique route binding".format(
                    expected[index][0]))


def main():
    parser = argparse.ArgumentParser()
    release_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--worker-root", type=Path,
        help="bind the external automation_system_v0101 implementation")
    arguments = parser.parse_args()
    module = _load_e2e(release_root)
    _assert_transition_route_contract(module, release_root)
    if arguments.worker_root is None:
        _bind_release_contract(module, release_root)
        config_path = release_root / "runtime" / "config" \
            / "device_state_data_v9_node_fn-01.json"
        contract_label = "packaged canonical contract"
    else:
        worker_root = arguments.worker_root.expanduser().resolve()
        if not worker_root.is_dir() or worker_root.is_symlink():
            raise RuntimeError(
                "real automation_system_v0101 source root is unavailable")
        module._bind_worker_storage_verifier(worker_root)
        config_path = worker_root / "_config" / "config_data" \
            / "device_state_data_v9_node_fn-01.json"
        contract_label = "real automation_system_v0101 contract"
    baseline_storage = json.loads(config_path.read_text(encoding="utf-8"))
    module._require_v9_storage(baseline_storage, "real flat V9 baseline")
    baseline_runtime = module._require_v9_runtime(
        module._NORMALIZE_RUNTIME(
            baseline_storage, representation="storage"),
        "real grouped V9 baseline")
    assert all("rows" in group for group in baseline_runtime["sensors"])
    assert all("rows" not in row for row in baseline_storage["sensors"])

    actuator = module._verify_actuator_524_contract(baseline_storage)
    controller_uuid = actuator["controller_uuid"]
    structural_storage = copy.deepcopy(baseline_storage)
    controller = module._controller_by_uuid(
        structural_storage, controller_uuid)
    controller["tags"] = list(controller.get("tags") or ()) \
        + ["worker-file-e2e-wal-projection"]
    structural_runtime = module._NORMALIZE_RUNTIME(
        structural_storage, representation="storage")
    structural_wal, structural_baseline, structural_target, \
        structural_actions = _wal(
            module, {
                "baseline": baseline_runtime,
                "candidate": structural_runtime,
            }, "structural")
    assert structural_actions is None
    structural = module._validated_wal_evidence(structural_wal, {
        "baseline": structural_baseline,
        "target": structural_target,
        "authority": {
            "scope": "worker_node_local",
            "route": "structural",
            "node_id": "fn-01",
        },
    })
    assert structural["mode"] == "node_local"
    assert structural["candidate_fingerprint"] \
        == module._fingerprint(structural_storage)

    policy_storage = copy.deepcopy(baseline_storage)
    policy_state = module._state(policy_storage, 21)
    policy_state.setdefault("parameters", {})["target_temp_c"] += 1.25
    policy_runtime = module._NORMALIZE_RUNTIME(
        policy_storage, representation="storage")
    policy_wal, policy_baseline, policy_target, policy_actions = _wal(
        module, {
            "baseline": baseline_runtime,
            "candidate": policy_runtime,
        }, "policy_fallback")
    assert policy_actions is not None and list(policy_actions)
    policy = module._validated_wal_evidence(policy_wal, {
        "baseline": policy_baseline,
        "target": policy_target,
        "authority": {
            "scope": "worker_node_local",
            "route": "policy_fallback",
            "node_id": "fn-01",
        },
    })
    assert policy["mode"] == "node_local"
    assert policy["file_route"] == "policy_fallback"

    runtime_storage = copy.deepcopy(baseline_storage)
    state = module._state(runtime_storage, 1)
    state["value_id"] = _alternate_value(
        runtime_storage, state.get("value_id"))
    runtime_runtime = module._NORMALIZE_RUNTIME(
        runtime_storage, representation="storage")
    runtime_wal, runtime_baseline, runtime_target, runtime_actions = _wal(
        module, {
            "baseline": baseline_runtime,
            "candidate": runtime_runtime,
        }, "runtime")
    assert runtime_actions is not None and list(runtime_actions)
    runtime = module._validated_wal_evidence(runtime_wal, {
        "baseline": runtime_baseline,
        "target": runtime_target,
        "authority": {
            "scope": "worker_node_local",
            "route": "runtime",
            "node_id": "fn-01",
        },
    })
    assert runtime["mode"] == "node_local"
    assert runtime["differential_operation"] == "action_plan"

    flat_wal = copy.deepcopy(structural_wal)
    flat_wal["baseline_config"] = copy.deepcopy(structural_target)
    authority = {
        "scope": "worker_node_local",
        "route": "structural",
        "node_id": "fn-01",
    }
    _expect_rejected(module, {
        "wal": flat_wal,
        "baseline": structural_baseline,
        "target": structural_target,
    }, authority)
    projection_drift = copy.deepcopy(structural_wal)
    _drift_first_sensor(projection_drift["baseline_config"])
    _expect_rejected(module, {
        "wal": projection_drift,
        "baseline": structural_baseline,
        "target": structural_target,
    }, authority)
    journal_drift = copy.deepcopy(structural_wal)
    journal_drift["last_operation"]["authority_commit"][
        "scope_revision"] = 12
    _expect_rejected(module, {
        "wal": journal_drift,
        "baseline": structural_baseline,
        "target": structural_target,
    }, authority)
    print(
        "PASS {} grouped File-WAL -> flat canonical V9 proof: "
        "node_local structural + policy_fallback + runtime + "
        "fail-closed representation drift".format(
            contract_label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
